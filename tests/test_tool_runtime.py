"""Behavior checks for the offline executor; no online model is used."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import tool_runtime as tools


class ToolRuntimeTests(unittest.TestCase):
    def call(self, arguments='{"path":"package.json"}', name="read_file"):
        return tools.ToolCall("call_1", name, arguments)

    def spy_runtime(self):
        runtime = tools.ToolRuntime()
        handler = Mock(return_value={"text": "ok"})
        definition = tools.ToolDefinition("read_file", "Read text", (tools.StringParameter("path", "File name"),), handler)
        runtime.register(definition)
        return runtime, handler, definition

    def test_success_preserves_identity_and_actual_file_content(self):
        context = tools.fixture_context()
        runtime = tools.make_runtime()
        result = runtime.execute(self.call(), context)
        self.assertTrue(result["ok"])
        self.assertEqual(result["call_id"], "call_1")
        self.assertEqual(result["tool"], "read_file")
        text = context.files["package.json"].read_text(encoding="utf-8")
        self.assertEqual(result["value"]["content"], text)
        self.assertEqual(result["value"]["bytes_read"], len(text.encode("utf-8")))

    def test_duplicate_registration_does_not_replace_original_tool(self):
        runtime, handler, definition = self.spy_runtime()
        with self.assertRaisesRegex(ValueError, "Duplicate tool"):
            runtime.register(definition)
        runtime.execute(self.call(), tools.fixture_context())
        handler.assert_called_once()

    def test_invalid_arguments_never_reach_handler(self):
        cases = ["{broken", "[]", "{}", '{"path":42}', '{"path":""}',
                 '{"path":"package.json","permission":"allow"}',
                 json.dumps({"path": "x" * 257})]
        for arguments in cases:
            with self.subTest(arguments=arguments[:60]):
                runtime, handler, _ = self.spy_runtime()
                result = runtime.execute(self.call(arguments), tools.fixture_context())
                self.assertFalse(result["ok"])
                self.assertIn(result["error"]["code"], ("INVALID_JSON", "INVALID_ARGUMENTS"))
                handler.assert_not_called()

    def test_hidden_tool_is_denied_even_when_execute_is_called_directly(self):
        runtime, handler, _ = self.spy_runtime()
        context = tools.fixture_context(allowed_tools=frozenset())
        self.assertEqual(runtime.catalog(context), [])
        result = runtime.execute(self.call(), context)
        self.assertEqual(result["error"]["code"], "TOOL_DENIED")
        handler.assert_not_called()

    def test_resource_policy_is_enforced_by_the_read_operation(self):
        context = tools.fixture_context()
        for name in ("../private.txt", str(context.files["package.json"]), "package.json:stream"):
            with self.subTest(name=name):
                with self.assertRaises(tools.ToolFailure) as error:
                    tools.read_registered_text(name, context)
                self.assertEqual(error.exception.code, "FILE_DENIED")

    def test_limits_count_bytes_and_accept_the_exact_boundary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "text.txt"
            path.write_bytes("你好".encode("utf-8"))
            context = tools.fixture_context(files={"package.json": path}, max_read_bytes=6)
            success = tools.make_runtime().execute(self.call(), context)
            self.assertTrue(success["ok"])
            context.max_read_bytes = 5
            failure = tools.make_runtime().execute(self.call(), context)
            self.assertEqual(failure["error"]["code"], "FILE_TOO_LARGE")
            context.max_argument_bytes = 1
            failure = tools.make_runtime().execute(self.call(), context)
            self.assertEqual(failure["error"]["code"], "ARGUMENTS_TOO_LARGE")

    def test_expired_or_cancelled_calls_never_start_the_handler(self):
        cancelled = Event()
        cancelled.set()
        for context, expected in [
            (tools.fixture_context(deadline=monotonic() - 1), "TOOL_TIMEOUT"),
            (tools.fixture_context(cancelled=cancelled), "CANCELLED"),
        ]:
            with self.subTest(expected=expected):
                runtime, handler, _ = self.spy_runtime()
                result = runtime.execute(self.call(), context)
                self.assertEqual(result["error"]["code"], expected)
                handler.assert_not_called()

    def test_cancellation_observed_after_body_return_is_not_success(self):
        runtime = tools.ToolRuntime()
        def body(args, context):
            context.cancelled.set()
            return {"text": "finished"}
        runtime.register(tools.ToolDefinition("read_file", "Read", (tools.StringParameter("path", "Name"),), body))
        result = runtime.execute(self.call(), tools.fixture_context())
        self.assertEqual(result["error"]["code"], "CANCELLED")

    def test_internal_bug_has_a_diagnostic_but_not_a_model_traceback(self):
        runtime, handler, _ = self.spy_runtime()
        handler.side_effect = RuntimeError("developer-only detail")
        with self.assertLogs(tools.LOGGER.name, level="ERROR") as diagnostics:
            result = runtime.execute(self.call(), tools.fixture_context())
        self.assertEqual(result["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("developer-only detail", json.dumps(result))
        self.assertIn("developer-only detail", "\n".join(diagnostics.output))


if __name__ == "__main__":
    unittest.main()
