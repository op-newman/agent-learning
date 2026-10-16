"""Behavior checks for lesson 6; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import reliable_loop as loop


class StreamAssemblyTests(unittest.TestCase):
    def test_stream_assembly_keeps_text_and_usage_until_terminal_finish(self):
        chunks = [
            loop.BlockStart(0, "text"),
            loop.TextDelta(0, "hello "),
            loop.TextDelta(0, "world"),
            loop.BlockEnd(0, {"type": "text", "text": "hello world"}),
            loop.UsageChunk(loop.TokenUsage(7, 2, 9)),
            loop.FinishChunk(loop.FinishReason("stop")),
        ]
        result = loop.assemble_chunks(chunks)
        self.assertEqual(result.blocks, ({"type": "text", "text": "hello world"},))
        self.assertEqual(result.usage.total_tokens, 9)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.event_type, "assistant/message")

    def test_stream_without_finish_is_rejected(self):
        with self.assertRaises(loop.LoopError) as error:
            loop.assemble_chunks([loop.TextDelta(0, "partial")])
        self.assertEqual(error.exception.code, "MISSING_TERMINAL_FINISH")

    def test_stream_rejects_chunks_after_finish(self):
        with self.assertRaises(loop.LoopError) as error:
            loop.assemble_chunks([
                loop.FinishChunk(loop.FinishReason("stop")),
                loop.TextDelta(0, "too late"),
            ])
        self.assertEqual(error.exception.code, "CHUNKS_AFTER_FINISH")

    def test_max_tokens_drops_a_truncated_tool_call(self):
        chunks = [
            loop.BlockStart(0, "text"),
            loop.TextDelta(0, "I started an edit."),
            loop.BlockStart(1, "tool-call"),
            loop.ToolCallDelta(1, "call-1", "edit", '{"path":"a.py",'),
            loop.FinishChunk(loop.FinishReason("max-tokens")),
        ]
        result = loop.assemble_chunks(chunks)
        self.assertEqual([block["type"] for block in result.blocks], ["text"])
        self.assertTrue(result.succeeded)

    def test_interrupted_blocks_keep_visible_text_but_not_tool_calls(self):
        chunks = [
            loop.BlockStart(0, "reasoning"),
            loop.ReasoningDelta(0, "checking"),
            loop.BlockStart(1, "text"),
            loop.TextDelta(1, "partial answer"),
            loop.BlockStart(2, "tool-call"),
            loop.ToolCallDelta(2, "call-1", "read", '{"path":'),
        ]
        self.assertEqual(
            loop.interrupted_blocks(chunks),
            (
                {"type": "reasoning", "text": "checking"},
                {"type": "text", "text": "partial answer"},
            ),
        )


class RetryTests(unittest.TestCase):
    def test_transient_failure_retries_and_success_becomes_surface_message(self):
        scripts = [
            [loop.FinishChunk(loop.FinishReason("error", "SERVER", "temporary"))],
            [loop.TextDelta(0, "ok"), loop.FinishChunk(loop.FinishReason("stop"))],
        ]
        result = loop.run_attempt_with_retry(lambda number: scripts[number - 1])
        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            [event["type"] for event in result.events],
            ["assistant/attempt", "llm/retry", "llm/retry-started", "assistant/message"],
        )

    def test_retry_is_planned_before_the_wait(self):
        seen = []
        scripts = [
            [loop.FinishChunk(loop.FinishReason("error", "TIMEOUT"))],
            [loop.FinishChunk(loop.FinishReason("stop"))],
        ]

        def fake_sleep(delay, token):
            seen.append(("sleep", delay, token.cancelled))

        result = loop.run_attempt_with_retry(
            lambda number: scripts[number - 1],
            sleep=fake_sleep,
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(seen, [("sleep", 500, False)])
        self.assertIn("llm/retry", [event["type"] for event in result.events])

    def test_non_retryable_failure_stops_without_a_retry_event(self):
        result = loop.run_attempt_with_retry(
            lambda number: [loop.FinishChunk(loop.FinishReason(
                "error", "AUTHENTICATION", "bad key",
            ))],
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.attempts, 1)
        self.assertNotIn("llm/retry", [event["type"] for event in result.events])
        self.assertEqual(result.settlement.event_type, "assistant/attempt")

    def test_always_mode_retries_every_failure_until_success(self):
        scripts = [
            [loop.FinishChunk(loop.FinishReason("error", "AUTHENTICATION"))],
            [loop.FinishChunk(loop.FinishReason("error", "SERVER"))],
            [loop.FinishChunk(loop.FinishReason("stop"))],
        ]
        result = loop.run_attempt_with_retry(
            lambda number: scripts[number - 1],
            policy=loop.RetryPolicy(mode="always", max_retries=0),
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 3)

    def test_backoff_is_exponential_capped_and_symmetric(self):
        policy = loop.RetryPolicy(
            initial_delay_ms=500, max_delay_ms=1000, jitter_ratio=0.1,
        )
        self.assertEqual(loop.retry_delay_ms(policy, 1, 0.5), 500)
        self.assertEqual(loop.retry_delay_ms(policy, 2, 0.5), 1000)
        self.assertEqual(loop.retry_delay_ms(policy, 3, 0.5), 1000)
        self.assertEqual(loop.retry_delay_ms(policy, 1, 0.0), 450)
        self.assertEqual(loop.retry_delay_ms(policy, 1, 1.0), 550)

    def test_cancellation_during_a_stream_is_not_retried(self):
        token = loop.CancellationToken()
        calls = []

        def stream_factory(number):
            calls.append(number)
            yield loop.TextDelta(0, "visible before stop")
            token.cancel()
            yield loop.TextDelta(0, "not delivered")

        result = loop.run_attempt_with_retry(stream_factory, cancel_token=token)
        self.assertTrue(result.cancelled)
        self.assertEqual(calls, [1])
        self.assertEqual(result.delivered_text, "visible before stop")
        self.assertNotIn("llm/retry", [event["type"] for event in result.events])

    def test_cancellation_during_backoff_stops_before_retry_started(self):
        token = loop.CancellationToken()
        scripts = [[loop.FinishChunk(loop.FinishReason("error", "SERVER"))]]

        def fake_sleep(delay, signal):
            signal.cancel()

        result = loop.run_attempt_with_retry(
            lambda number: scripts[number - 1],
            cancel_token=token,
            sleep=fake_sleep,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(
            [event["type"] for event in result.events],
            ["assistant/attempt", "llm/retry"],
        )


class ToolSchedulingTests(unittest.TestCase):
    def test_parallel_safe_calls_overlap_up_to_the_configured_limit(self):
        calls = [
            loop.ToolCallSpec("a", "read", "parallel-safe", 2),
            loop.ToolCallSpec("b", "read", "parallel-safe", 2),
            loop.ToolCallSpec("c", "read", "parallel-safe", 2),
        ]
        timeline = loop.schedule_tool_calls(calls, 2)
        self.assertLessEqual(loop.max_observed_concurrency(timeline), 2)
        self.assertEqual(timeline[0].start, timeline[1].start)
        self.assertEqual(timeline[2].start, 2)

    def test_exclusive_call_is_a_barrier_for_both_sides(self):
        calls = [
            loop.ToolCallSpec("read", "read", "parallel-safe", 2),
            loop.ToolCallSpec("write", "write", "exclusive", 1),
            loop.ToolCallSpec("test", "bash", "parallel-safe", 1),
        ]
        timeline = loop.schedule_tool_calls(calls, 2)
        self.assertEqual(timeline[0], loop.ScheduledToolCall("read", 0.0, 2.0))
        self.assertEqual(timeline[1], loop.ScheduledToolCall("write", 2.0, 3.0))
        self.assertGreaterEqual(timeline[2].start, 3.0)

    def test_scheduler_rejects_invalid_parallelism_and_mode(self):
        call = loop.ToolCallSpec("a", "read", "parallel-safe", 1)
        with self.assertRaises(loop.LoopError) as error:
            loop.schedule_tool_calls([call], 0)
        self.assertEqual(error.exception.code, "INVALID_PARALLELISM")
        with self.assertRaises(loop.LoopError) as error:
            loop.schedule_tool_calls([loop.ToolCallSpec("a", "a", "unknown", 1)], 1)
        self.assertEqual(error.exception.code, "INVALID_EXECUTION_MODE")


class ReconciliationTests(unittest.TestCase):
    def test_unfinished_tools_get_distinct_recovery_results(self):
        calls = [
            loop.ToolCallSpec("aborted", "read", "parallel-safe", 1),
            loop.ToolCallSpec("started", "write", "exclusive", 1),
            loop.ToolCallSpec("never-started", "bash", "parallel-safe", 1),
        ]
        reconciled = loop.reconcile_unfinished_tools(
            calls,
            started_ids={"started"},
            results={},
            aborted_ids={"aborted"},
        )
        self.assertEqual(reconciled["aborted"]["code"], "ABORTED_BEFORE_DISPATCH")
        self.assertEqual(reconciled["started"]["code"], "UNKNOWN_OUTCOME")
        self.assertEqual(reconciled["never-started"]["code"], "INTERRUPTED_BEFORE_START")
        self.assertTrue(all(result["is_error"] for result in reconciled.values()))

    def test_completed_results_are_not_overwritten(self):
        calls = [loop.ToolCallSpec("a", "read", "parallel-safe", 1)]
        existing = {"a": {"is_error": False, "content": "done"}}
        reconciled = loop.reconcile_unfinished_tools(calls, {"a"}, existing)
        self.assertEqual(reconciled["a"], existing["a"])


if __name__ == "__main__":
    unittest.main()