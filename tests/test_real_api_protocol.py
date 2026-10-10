"""Local HTTP integration checks. The server simulates API responses, not an LLM."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import chat_completions_agent as agent


def tool_response(arguments='{"path":"package.json"}', finish="tool_calls"):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "read_123", "type": "function",
            "function": {"name": "read_file", "arguments": arguments},
        }],
    }}]}


def text_response(text):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": text,
    }}]}


@contextmanager
def fake_api(responses):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "body": body})
            index = len(received) - 1
            status, payload = responses[index] if index < len(responses) else (500, {})
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class ProtocolTests(unittest.TestCase):
    def test_two_requests_carry_the_real_tool_result_and_call_id(self):
        with fake_api([(200, tool_response()), (200, text_response("learning-demo 0.1.0"))]) as (url, calls):
            result = agent.run_agent(url, "test-model", "fake-test-key")
        self.assertEqual(result["status"], "turn_ended")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["path"], "/v1/chat/completions")
        self.assertEqual(calls[0]["authorization"], "Bearer fake-test-key")
        first = calls[0]["body"]
        self.assertEqual(first["model"], "test-model")
        self.assertFalse(first["stream"])
        self.assertEqual(first["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(len(first["messages"]), 2)
        history = calls[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in history], ["system", "user", "assistant", "tool"])
        self.assertEqual(history[3]["tool_call_id"], history[2]["tool_calls"][0]["id"])
        observed = json.loads(history[3]["content"])
        self.assertTrue(observed["ok"])
        self.assertEqual(observed["content"], agent.FIXTURE.read_text(encoding="utf-8"))

    def test_step_limit_does_not_claim_an_answer_or_send_another_request(self):
        with fake_api([(200, tool_response())]) as (url, calls):
            result = agent.run_agent(url, "test-model", "fake-test-key", max_steps=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {"status": "step_limit", "answer": None, "model_calls": 1})

    def test_invalid_tool_arguments_are_observed_by_the_next_request(self):
        with fake_api([(200, tool_response("{broken")), (200, text_response("Cannot read yet"))]) as (url, calls):
            agent.run_agent(url, "test-model", "fake-test-key")
        observation = json.loads(calls[1]["body"]["messages"][-1]["content"])
        self.assertFalse(observation["ok"])
        self.assertIn("error", observation)

    def test_unauthorized_http_response_stops_without_retry(self):
        with fake_api([(401, {"error": {"message": "bad key"}})]) as (url, calls):
            with self.assertRaisesRegex(agent.ApiError, "HTTP 401"):
                agent.run_agent(url, "test-model", "fake-test-key")
        self.assertEqual(len(calls), 1)

    def test_truncated_response_does_not_execute_a_tool(self):
        with fake_api([(200, tool_response(finish="length"))]) as (url, calls):
            with patch.object(agent, "execute_tool") as execute:
                with self.assertRaisesRegex(agent.ApiError, "length"):
                    agent.run_agent(url, "test-model", "fake-test-key")
                execute.assert_not_called()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
