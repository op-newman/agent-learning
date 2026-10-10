"""A teaching Agent for the text/function subset of Chat Completions.

Default: print a request preview. --live sends real HTTPS requests.
Only Python's standard library is needed. No automatic retries or streaming.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


FIXTURE = Path(__file__).resolve().parent / "fixture" / "package.json"
TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the teaching workspace's package.json as UTF-8 text.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "enum": ["package.json"]}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


class ApiError(Exception):
    """A transport failure or unsupported response; never a success answer."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep credentials at the configured endpoint.
        return None


def initial_history():
    return [
        {"role": "system", "content": "Read the file before answering. Use actual tool results."},
        {"role": "user", "content": "Read package.json and report the project name and version."},
    ]


def build_request(history, model):
    return {"model": model, "messages": history, "tools": [TOOL],
            "tool_choice": "auto", "stream": False}


def parse_response(payload):
    """Accept one standard text/function choice, keeping tool arguments as strings."""
    if not isinstance(payload, dict):
        raise ApiError("Response must be a JSON object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ApiError("This example expects exactly one choice.")
    choice = choices[0]
    finish = choice.get("finish_reason")
    if finish not in ("stop", "tool_calls"):
        raise ApiError(f"Generation did not finish normally: {finish!r}")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ApiError("Expected an assistant message.")
    if message.get("refusal"):
        raise ApiError("The model returned a refusal.")
    supported = {"role", "content", "tool_calls", "refusal"}
    for key, value in message.items():
        if key not in supported and value not in (None, "", [], {}):
            raise ApiError(f"Unsupported assistant field {key!r}; use a matching adapter.")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ApiError("This example accepts text content only.")
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise ApiError("tool_calls must be an array.")
    ids = set()
    normalized_calls = []
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ApiError("Only function tool calls are supported.")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            raise ApiError("Tool call ids must be non-empty and unique within a response.")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ApiError("A function tool call needs a name.")
        if not function["name"] or not isinstance(function.get("arguments"), str):
            raise ApiError("A function needs a non-empty name and raw JSON arguments.")
        ids.add(call_id)
        normalized_calls.append({"id": call_id, "type": "function", "function": {
            "name": function["name"], "arguments": function["arguments"],
        }})
    if (finish == "tool_calls") != bool(normalized_calls):
        raise ApiError("finish_reason and tool_calls disagree.")
    if not normalized_calls and not content:
        raise ApiError("The response contains neither an answer nor a tool call.")
    normalized = {"role": "assistant", "content": content}
    if normalized_calls:
        normalized["tool_calls"] = normalized_calls
    return normalized


def call_model(endpoint, api_key, body, timeout):
    """POST one complete JSON request; HTTP is allowed only for local tests."""
    url = urllib.parse.urlsplit(endpoint)
    local_http = url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    if (not url.hostname or (url.scheme != "https" and not local_http)
            or url.username or url.password or url.query or url.fragment):
        raise ApiError("Use a full HTTPS endpoint without URL credentials, query, or fragment.")
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + api_key},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ApiError(f"HTTP {status}: model request failed; no automatic retry.") from None
    except (urllib.error.URLError, OSError):
        raise ApiError("Network/TLS/timeout failure; no automatic retry.") from None
    except (ValueError, UnicodeError):
        raise ApiError("The endpoint did not return valid JSON.") from None
    return parse_response(payload)


def execute_tool(call):
    """Return expected tool failures as observations for the next model request."""
    function = call["function"]
    try:
        if function["name"] != "read_file":
            raise ValueError("Unknown tool")
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict) or set(arguments) != {"path"}:
            raise ValueError("Expected exactly one argument: path")
        if arguments["path"] != "package.json":
            raise ValueError("Only the fixture package.json may be read")
        result = {"ok": True, "content": FIXTURE.read_text(encoding="utf-8")}
    except (ValueError, OSError, UnicodeError) as error:
        result = {"ok": False, "error": str(error)}
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}


def run_agent(endpoint, model, api_key, max_steps=4, timeout=30):
    history = initial_history()
    for step in range(1, max_steps + 1):
        message = call_model(endpoint, api_key, build_request(history, model), timeout)
        history.append(message)
        calls = message.get("tool_calls", [])
        if not calls:
            return {"status": "turn_ended", "answer": message["content"], "model_calls": step}
        for call in calls:
            history.append(execute_tool(call))
    return {"status": "step_limit", "answer": None, "model_calls": max_steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send requests to the configured API")
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.max_steps < 1 or args.timeout <= 0:
        parser.error("--max-steps and --timeout must be positive")
    if not args.live:
        print("PREVIEW ONLY: no network request and no API key required.")
        print(json.dumps(build_request(initial_history(), "YOUR_MODEL_ID"), indent=2))
        return
    endpoint = os.environ.get("AGENT_API_URL", "").strip()
    model = os.environ.get("AGENT_MODEL", "").strip()
    if not endpoint or not model:
        parser.error("Set AGENT_API_URL (full endpoint) and AGENT_MODEL before --live.")
    key = os.environ.get("AGENT_API_KEY") or getpass.getpass("API key (hidden): ")
    if not key.strip():
        parser.error("An API key is required.")
    try:
        result = run_agent(endpoint, model, key, args.max_steps, args.timeout)
    except ApiError as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
