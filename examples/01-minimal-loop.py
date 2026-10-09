"""Teaching demo: a deterministic mock model, one real tool, and a bounded loop.

No network or API key. The message format is local to this demo, not a vendor API.
"""

import argparse
import json
from pathlib import Path


FIXTURE = Path(__file__).resolve().parent / "fixture" / "package.json"
TOOL_DEFINITIONS = [{
    "name": "read_file",
    "description": "Read the teaching workspace's package.json as UTF-8 text.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "enum": ["package.json"]}},
        "required": ["path"],
        "additionalProperties": False,
    },
}]


def read_file(path):
    """Read only the fixed fixture; model input never chooses a host path."""
    if path != "package.json":
        raise ValueError("This tool only allows package.json")
    return FIXTURE.read_text(encoding="utf-8")


TOOLS = {"read_file": read_file}


def execute_tool(call):
    """Validate external-style arguments and convert expected failures to data."""
    try:
        name = call["name"]
        if name not in TOOLS:
            raise ValueError(f"Unknown tool: {name}")
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict) or set(arguments) != {"path"}:
            raise ValueError("Arguments must contain exactly one field: path")
        if not isinstance(arguments["path"], str):
            raise ValueError("path must be a string")
        return {"ok": True, "content": TOOLS[name](**arguments)}
    except (ValueError, OSError, UnicodeError) as error:
        return {"ok": False, "error": str(error)}


def call_model(history, tool_definitions):
    """Simulate two responses using fixed rules, with no actual LLM inference."""
    if history[-1]["role"] != "tool":
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "name": tool_definitions[0]["name"],
                "arguments": json.dumps({"path": "package.json"}),
            }],
        }

    result = json.loads(history[-1]["content"])
    if not result["ok"]:
        answer = f"The file could not be read: {result['error']}"
    else:
        try:
            package = json.loads(result["content"])
            if not isinstance(package, dict):
                raise ValueError("package.json must contain an object")
            answer = f"Project: {package['name']}; version: {package['version']}."
        except (ValueError, KeyError) as error:
            answer = f"The file was read, but its project data is invalid: {error}"
    return {"role": "assistant", "content": answer, "tool_calls": []}


def run_agent(max_steps):
    """Print the input history at each call and return an explicit stop reason."""
    history = [
        {"role": "system", "content": "Answer from actual file-reading results."},
        {"role": "user", "content": "Read package.json and report its name and version."},
    ]
    print("MOCK MODEL DEMO: fixed decisions, real file read, no network.")
    print("Tool definitions:", json.dumps(TOOL_DEFINITIONS))

    for step in range(1, max_steps + 1):
        print(f"\n=== Model call {step}: {len(history)} input messages ===")
        print(json.dumps(history, indent=2))
        response = call_model(history, TOOL_DEFINITIONS)
        print("Model response:", json.dumps(response))
        history.append(response)

        if not response["tool_calls"]:
            # This means the turn ended; it is not an independent task verifier.
            return {"status": "completed", "answer": response["content"],
                    "history_messages": len(history)}

        for call in response["tool_calls"]:
            result = execute_tool(call)
            print("Tool result:", json.dumps(result))
            history.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result),
            })

    return {"status": "step_limit", "answer": None,
            "history_messages": len(history)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-steps", type=int, default=4)
    args = parser.parse_args()
    if args.max_steps < 1:
        parser.error("--max-steps must be at least 1")
    print("\nFinal state:", json.dumps(run_agent(args.max_steps), indent=2))
