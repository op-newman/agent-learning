"""Behavior checks for lesson 4; no model, network, or real filesystem is used."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import session_log as sessions


def build_complete_session():
    durable = sessions.DurableLog()
    log = sessions.SessionLog(clock=lambda: 1_700_000_000_000)
    log.append("turn/start", {"turn": 0})
    log.append("step/start", {"turn": 0, "step": 0})
    log.append("system/message", {"turn": 0, "step": 0, "text": "rules"})
    log.append("user/message", {"turn": 0, "step": 0, "text": "read package.json"})
    executor = sessions.ToolExecutor({"read_file": lambda args: "content"})
    sessions.execute_model_step(
        log, durable, 0, 0, "reading",
        [{"id": "call_1", "name": "read_file", "arguments": json.dumps({"path": "package.json"})}],
        executor,
    )
    log.append("step/end", {"turn": 0, "step": 0})
    log.append("turn/end", {"turn": 0, "reason": {"kind": "success"}})
    log.checkpoint(durable)
    return log, durable, executor


def crash_session(crash_at, handler):
    durable = sessions.DurableLog()
    log = sessions.SessionLog(clock=lambda: 1_700_000_000_000)
    log.append("turn/start", {"turn": 0})
    log.append("step/start", {"turn": 0, "step": 0})
    log.append("user/message", {"turn": 0, "step": 0, "text": "write result.txt"})
    executor = sessions.ToolExecutor({"write_file": handler})
    try:
        sessions.execute_model_step(
            log, durable, 0, 0, "writing",
            [{"id": "call_1", "name": "write_file", "arguments": json.dumps({"path": "result.txt"})}],
            executor, crash_at=crash_at,
        )
    except sessions.SimulatedCrash:
        pass
    else:
        raise AssertionError("expected a simulated crash")
    restored, closers = sessions.recover(durable)
    codes = [closer["data"]["error_code"] for closer in closers if closer["type"] == "tool/result"]
    return restored, executor, codes


class SessionLogTests(unittest.TestCase):
    def test_seq_is_contiguous_and_append_returns_a_snapshot(self):
        log = sessions.SessionLog(clock=lambda: 7)
        event = log.append("user/message", {"text": "hello"})
        event["data"]["text"] = "changed"
        self.assertEqual(log.events[0]["data"]["text"], "hello")
        self.assertEqual(log.events[0]["seq"], 0)
        second = log.append("assistant/message", {"text": "hi", "tool_calls": []})
        self.assertEqual(second["seq"], 1)
        self.assertEqual([item["seq"] for item in log.events], [0, 1])

    def test_non_json_payload_is_rejected(self):
        log = sessions.SessionLog()
        with self.assertRaises(sessions.LogViolation) as error:
            log.append("user/message", {"text": float("nan")})
        self.assertEqual(error.exception.code, "NOT_JSON")

    def test_restore_rejects_a_gap_in_sequence_numbers(self):
        with self.assertRaises(sessions.LogViolation) as error:
            sessions.SessionLog([
                {"seq": 0, "time": 1, "type": "turn/start", "data": {"turn": 0}},
                {"seq": 2, "time": 1, "type": "step/start", "data": {"turn": 0, "step": 0}},
            ])
        self.assertEqual(error.exception.code, "BROKEN_SEQUENCE")

    def test_projection_keeps_surface_events_in_order(self):
        log, _, _ = build_complete_session()
        messages = log.derive_messages()
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(messages[3]["tool_call_id"], "call_1")
        self.assertFalse(messages[3]["is_error"])

    def test_log_only_events_do_not_reach_the_model(self):
        log, _, _ = build_complete_session()
        event_types = [event["type"] for event in log.events]
        self.assertIn("tool/call", event_types)
        self.assertNotIn("tool/call", sessions.SURFACE_TYPES)
        self.assertEqual(len(log.derive_messages()), 4)

    def test_committed_prefix_cannot_be_rewritten(self):
        durable = sessions.DurableLog()
        first = sessions.SessionLog(clock=lambda: 1)
        first.append("user/message", {"text": "first"})
        first.checkpoint(durable)
        replacement = sessions.SessionLog(clock=lambda: 1)
        replacement.append("user/message", {"text": "different"})
        with self.assertRaises(sessions.LogViolation) as error:
            replacement.checkpoint(durable)
        self.assertEqual(error.exception.code, "PREFIX_REWRITTEN")
        self.assertEqual([event["data"]["text"] for event in durable.replay()], ["first"])

    def test_crash_before_call_checkpoint_recovers_not_started(self):
        restored, executor, codes = crash_session(
            sessions.CRASH_BEFORE_CALL_CHECKPOINT, lambda args: "written")
        self.assertEqual(executor.body_invocations, 0)
        self.assertEqual(codes, ["TOOL_NOT_STARTED"])
        self.assertEqual(executor.body_invocations, 0)

    def test_crash_after_call_checkpoint_recovers_outcome_unknown(self):
        restored, executor, codes = crash_session(
            sessions.CRASH_AFTER_CALL_CHECKPOINT, lambda args: "written")
        self.assertEqual(executor.body_invocations, 0)
        self.assertEqual(codes, ["TOOL_OUTCOME_UNKNOWN"])
        self.assertEqual(executor.body_invocations, 0)

    def test_crash_after_result_append_does_not_replay_the_body(self):
        restored, executor, codes = crash_session(
            sessions.CRASH_AFTER_RESULT_APPEND, lambda args: "written")
        self.assertEqual(executor.body_invocations, 1)
        self.assertEqual(codes, ["TOOL_OUTCOME_UNKNOWN"])
        self.assertEqual(executor.body_invocations, 1)
        self.assertIn("Do not retry blindly.", restored.derive_messages()[-1]["content"])

    def test_recovery_balances_the_tail_and_is_idempotent(self):
        restored, _, _ = crash_session(sessions.CRASH_AFTER_CALL_CHECKPOINT, lambda args: "written")
        self.assertEqual(restored.events[0]["seq"], 0)
        self.assertEqual([event["seq"] for event in restored.events], list(range(len(restored.events))))
        self.assertEqual(restored.events[-2]["type"], "step/end")
        self.assertEqual(restored.events[-1]["type"], "turn/end")
        self.assertEqual(restored.events[-1]["data"]["reason"]["kind"], "interrupted")
        self.assertEqual(sessions.interrupted_turn_closers(restored.events), [])
        self.assertEqual(restored.derive_messages()[-1]["role"], "tool")
        self.assertTrue(restored.derive_messages()[-1]["is_error"])

    def test_a_result_without_a_durable_call_is_still_matched(self):
        events = [
            {"seq": 0, "time": 1, "type": "turn/start", "data": {"turn": 0}},
            {"seq": 1, "time": 1, "type": "step/start", "data": {"turn": 0, "step": 0}},
            {"seq": 2, "time": 1, "type": "assistant/message", "data": {
                "turn": 0, "step": 0, "text": "",
                "tool_calls": [{"id": "call_1", "name": "write_file", "arguments": "{}"}],
            }},
        ]
        closers = sessions.interrupted_turn_closers(events)
        self.assertEqual([item["data"]["error_code"] for item in closers if item["type"] == "tool/result"],
                         ["TOOL_NOT_STARTED"])
        self.assertEqual([item["type"] for item in closers], ["tool/result", "step/end", "turn/end"])


if __name__ == "__main__":
    unittest.main()
