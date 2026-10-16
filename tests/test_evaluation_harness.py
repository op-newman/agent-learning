"""Behavior checks for lesson 10; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import evaluation_harness as ev


def write_case(case_id="writes", turns=None, checks=(), files=None, **kwargs):
    fixture = ev.Fixture("fixture-" + case_id, tuple(turns or ()))
    return ev.Case(id=case_id, fixture=fixture, checks=tuple(checks),
                   files=dict(files or {}), **kwargs)


class RecordingTests(unittest.TestCase):
    def test_turns_are_replayed_in_fifo_order(self):
        fixture = ev.Fixture("f", (
            ev.ModelTurn(tool_calls=(ev.ToolCall("read_file", {"path": "a"}),)),
            ev.ModelTurn(text="done"),
        ))
        model = ev.ScriptedModel(fixture)
        self.assertEqual(model.next_turn().tool_calls[0].name, "read_file")
        self.assertEqual(model.next_turn().text, "done")
        self.assertEqual(model.consumed, 2)

    def test_running_out_of_recorded_turns_is_an_error(self):
        model = ev.ScriptedModel(ev.Fixture("empty", ()))
        with self.assertRaises(ev.FixtureExhausted):
            model.next_turn()

    def test_each_run_gets_its_own_cursor(self):
        fixture = ev.Fixture("f", (ev.ModelTurn(text="hi"),))
        first = ev.ScriptedModel(fixture.copy())
        second = ev.ScriptedModel(fixture.copy())
        first.next_turn()
        self.assertEqual(second.consumed, 0)
        self.assertEqual(second.next_turn().text, "hi")

    def test_every_request_is_recorded_before_the_turn_is_handed_out(self):
        model = ev.ScriptedModel(ev.Fixture("f", (ev.ModelTurn(text="hi"),)))
        model.next_turn()
        self.assertEqual(model.requests, [{"turn": 0}])


class ToolRegistryTests(unittest.TestCase):
    def registry(self):
        return ev.ToolRegistry(ev.Workspace())

    def test_known_tools_are_sorted_by_name(self):
        registry = self.registry()
        for name, handler in ev.DEFAULT_TOOLS:
            registry.register(name, handler)
        self.assertEqual(registry.known(), ["claim_write", "read_file", "write_file"])

    def test_unknown_tool_is_a_result_not_a_crash(self):
        result = self.registry().call("ghost", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])

    def test_handler_failure_becomes_an_error_result(self):
        registry = self.registry()
        registry.register("read_file", ev.read_file)
        result = registry.call("read_file", {"path": "missing.txt"})
        self.assertFalse(result["ok"])
        self.assertIn("FileNotFoundError", result["error"])

    def test_every_call_is_traced_with_its_arguments(self):
        registry = self.registry()
        registry.register("read_file", ev.read_file)
        registry.call("read_file", {"path": "a"})
        self.assertEqual(registry.trace[0]["name"], "read_file")
        self.assertEqual(registry.trace[0]["args"], {"path": "a"})


class LoopTests(unittest.TestCase):
    def run_fixture(self, turns, files=None, max_tool_calls=8, tools=ev.DEFAULT_TOOLS):
        case = write_case(turns=turns, files=files, max_tool_calls=max_tool_calls,
                          tools=tools)
        return case.run().run

    def test_a_plain_script_completes_with_its_reply(self):
        result = self.run_fixture([ev.ModelTurn(text="hello")])
        self.assertEqual(result.stop_reason, ev.STOP_COMPLETED)
        self.assertEqual(result.reply, "hello")
        self.assertEqual(result.tool_trace, ())

    def test_tool_calls_execute_against_the_real_workspace(self):
        result = self.run_fixture([
            ev.ModelTurn(tool_calls=(ev.ToolCall(
                "write_file", {"path": "a.txt", "content": "x"}),)),
            ev.ModelTurn(text="done"),
        ])
        self.assertEqual(result.stop_reason, ev.STOP_COMPLETED)
        self.assertEqual([call["name"] for call in result.tool_trace], ["write_file"])

    def test_an_error_turn_settles_as_error(self):
        result = self.run_fixture([ev.ModelTurn(stop_reason=ev.STOP_ERROR,
                                                error="provider refused")])
        self.assertEqual(result.stop_reason, ev.STOP_ERROR)
        self.assertEqual(result.error, "provider refused")

    def test_running_past_the_fixture_is_an_error(self):
        result = self.run_fixture([])
        self.assertEqual(result.stop_reason, ev.STOP_ERROR)
        self.assertIn("fixture exhausted", result.error)

    def test_the_tool_budget_stops_a_runaway_loop(self):
        turns = [ev.ModelTurn(tool_calls=(ev.ToolCall("read_file", {"path": "a"}),))
                 for _ in range(5)]
        result = self.run_fixture(turns, files={"a": "x"}, max_tool_calls=2)
        self.assertEqual(result.stop_reason, ev.STOP_ERROR)
        self.assertEqual(result.error, "tool budget exhausted")
        self.assertEqual(len(result.tool_trace), 2)


class CheckTests(unittest.TestCase):
    def episode(self, turns, files=None, **kwargs):
        return write_case(turns=turns, files=files, **kwargs).run()

    def test_reply_contains(self):
        episode = self.episode([ev.ModelTurn(text="all done")])
        self.assertTrue(ev.reply_contains("done")(episode).ok)
        self.assertFalse(ev.reply_contains("failed")(episode).ok)

    def test_tool_sequence_compares_order(self):
        episode = self.episode([
            ev.ModelTurn(tool_calls=(ev.ToolCall("read_file", {"path": "a"}),)),
            ev.ModelTurn(text="done"),
        ], files={"a": "x"})
        self.assertTrue(ev.tool_sequence(["read_file"])(episode).ok)
        self.assertFalse(ev.tool_sequence([])(episode).ok)

    def test_tool_args_and_success(self):
        episode = self.episode([
            ev.ModelTurn(tool_calls=(ev.ToolCall("read_file", {"path": "a"}),)),
            ev.ModelTurn(text="done"),
        ], files={"a": "x"})
        self.assertTrue(ev.tool_args(0, {"path": "a"})(episode).ok)
        self.assertTrue(ev.tool_succeeded(0)(episode).ok)
        self.assertFalse(ev.tool_args(0, {"path": "b"})(episode).ok)

    def test_file_equals_reads_the_workspace_not_the_reply(self):
        episode = self.episode([
            ev.ModelTurn(tool_calls=(ev.ToolCall(
                "write_file", {"path": "a.txt", "content": "x"}),)),
            ev.ModelTurn(text="I wrote a.txt"),
        ])
        self.assertTrue(ev.file_equals("a.txt", "x")(episode).ok)
        self.assertFalse(ev.file_equals("a.txt", "y")(episode).ok)

    def test_file_unchanged_detects_a_stray_write(self):
        episode = self.episode([
            ev.ModelTurn(tool_calls=(ev.ToolCall(
                "write_file", {"path": "a.txt", "content": "changed"}),)),
            ev.ModelTurn(text="done"),
        ], files={"a.txt": "original"})
        self.assertFalse(ev.file_unchanged("a.txt")(episode).ok)

    def test_a_lying_tool_passes_the_reply_check_and_fails_the_file_check(self):
        episode = self.episode([
            ev.ModelTurn(tool_calls=(ev.ToolCall(
                "claim_write", {"path": "a.txt", "content": "x"}),)),
            ev.ModelTurn(text="Done, I wrote a.txt"),
        ])
        self.assertTrue(ev.reply_contains("Done")(episode).ok)
        self.assertFalse(ev.file_equals("a.txt", "x")(episode).ok)

    def test_stopped_with(self):
        episode = self.episode([ev.ModelTurn(text="hi")])
        self.assertTrue(ev.stopped_with(ev.STOP_COMPLETED)(episode).ok)
        self.assertFalse(ev.stopped_with(ev.STOP_ERROR)(episode).ok)


class SuiteTests(unittest.TestCase):
    def passing_case(self, case_id="pass"):
        return write_case(
            case_id=case_id,
            turns=[ev.ModelTurn(tool_calls=(ev.ToolCall(
                "write_file", {"path": "a.txt", "content": "x"}),)),
                   ev.ModelTurn(text="done")],
            checks=(ev.tool_sequence(["write_file"]),
                    ev.file_equals("a.txt", "x")),
        )

    def failing_case(self, case_id="fail"):
        return write_case(
            case_id=case_id,
            turns=[ev.ModelTurn(text="done")],
            checks=(ev.file_equals("a.txt", "x"),),
        )

    def test_a_passing_case_scores_one(self):
        outcome = ev.Suite([self.passing_case()]).run().outcomes[0]
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.score, 1.0)

    def test_score_is_the_share_of_passing_checks(self):
        outcome = ev.Suite([self.failing_case()]).run().outcomes[0]
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.score, 0.0)

    def test_the_gate_requires_every_case_to_pass(self):
        suite = ev.Suite([self.passing_case(), self.failing_case()])
        report = suite.run()
        self.assertLess(report.score, 1.0)
        self.assertFalse(report.gate())
        self.assertFalse(report.gate(0.75))

    def test_a_strict_bar_can_reject_a_high_scoring_suite(self):
        outcome = ev.Suite([self.passing_case()]).run()
        self.assertTrue(outcome.gate())
        self.assertFalse(outcome.gate(1.5))

    def test_each_case_runs_in_a_fresh_workspace(self):
        case = self.passing_case()
        first = ev.Suite([case]).run().outcomes[0]
        second = ev.Suite([case]).run().outcomes[0]
        self.assertEqual(first.episode.workspace_before, {})
        self.assertEqual(second.episode.workspace_before, {})

    def test_the_same_fixture_replays_identically(self):
        case = self.passing_case()
        first = ev.Suite([case]).run().outcomes[0]
        second = ev.Suite([case]).run().outcomes[0]
        self.assertEqual([c["name"] for c in first.episode.run.tool_trace],
                         [c["name"] for c in second.episode.run.tool_trace])

    def test_a_failure_report_names_the_failing_check_and_the_trace(self):
        report = ev.Suite([self.failing_case("bad")]).run().report()
        self.assertIn("case bad", report)
        self.assertIn("FAIL", report)
        self.assertIn("a.txt content", report)
        self.assertIn("observed trace", report)


if __name__ == "__main__":
    unittest.main()