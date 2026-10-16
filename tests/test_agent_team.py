"""Behavior checks for lesson 9; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import agent_team as team


ROOT = team.AgentHandle("session-root", depth=0, name="lead")


def echo_child(service, request):
    def handler(transcript):
        return f"reply to {transcript[-1]['text']!r}"
    return team.ContinuableChild(service, request, handler)


def build_service(max_depth=1, max_active=8):
    service = team.SubagentService(max_depth=max_depth, max_active_subagents=max_active)
    service.register(team.Provider("spawn", team.counting_handler))
    service.register(team.Provider(
        "deep",
        team.structured_handler,
        capabilities=frozenset({"agentOptions", "outputSchema", "depthLimit"}),
    ))
    service.register(team.Provider("resumable", team.echo_handler,
                                   continuable=echo_child))
    return service


def request(provider="spawn", prompt="hello", parent=ROOT, **kwargs):
    return team.StartRequest(provider=provider, prompt=prompt, parent=parent, **kwargs)


# ---------------------------------------------------------------------------
# Subagent seam
# ---------------------------------------------------------------------------

class CapabilityTests(unittest.TestCase):
    def test_request_without_optional_fields_needs_no_capability(self):
        result = build_service().start(request())
        self.assertEqual(result.value, "done: hello")
        self.assertTrue(result.ok)

    def test_missing_capability_is_not_silently_ignored(self):
        with self.assertRaises(team.SubagentError) as caught:
            build_service().start(request(output_schema={"type": "object"}))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")

    def test_declared_capability_allows_the_request(self):
        result = build_service().start(
            request(provider="deep", output_schema={"type": "object"})
        )
        self.assertEqual(result.structured, {"summary": "done: hello"})
        self.assertIsNone(result.value)

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(team.SubagentError) as caught:
            build_service().start(request(provider="ghost"))
        self.assertEqual(caught.exception.code, "UNKNOWN_PROVIDER")

    def test_output_schema_requires_a_mapping_result(self):
        service = build_service()
        service.register(team.Provider("scalar", lambda prompt: 41,
                                       capabilities=frozenset({"outputSchema"})))
        with self.assertRaises(team.SubagentError) as caught:
            service.start(request(provider="scalar", output_schema={"type": "object"}))
        self.assertEqual(caught.exception.code, "SCHEMA_MISMATCH")


class DepthTests(unittest.TestCase):
    def test_depth_one_allows_a_direct_child(self):
        self.assertTrue(build_service(max_depth=1).start(request()).ok)

    def test_depth_one_rejects_a_grandchild(self):
        child = team.AgentHandle("session-child", depth=1, name="child")
        with self.assertRaises(team.SubagentError) as caught:
            build_service(max_depth=1).start(request(parent=child))
        self.assertEqual(caught.exception.code, "DEPTH_LIMIT_EXCEEDED")

    def test_per_request_max_depth_overrides_the_default(self):
        # ``max_depth`` is itself a capability: the provider must declare it.
        child = team.AgentHandle("session-child", depth=1, name="child")
        result = build_service(max_depth=1).start(
            request(provider="deep", parent=child, max_depth=2)
        )
        self.assertTrue(result.ok)

    def test_per_request_max_depth_can_be_stricter(self):
        child = team.AgentHandle("session-child", depth=1, name="child")
        with self.assertRaises(team.SubagentError) as caught:
            build_service(max_depth=5).start(
                request(provider="deep", parent=child, max_depth=1)
            )
        self.assertEqual(caught.exception.code, "DEPTH_LIMIT_EXCEEDED")

    def test_depth_override_requires_the_capability_flag(self):
        child = team.AgentHandle("session-child", depth=1, name="child")
        with self.assertRaises(team.SubagentError) as caught:
            build_service(max_depth=1).start(request(parent=child, max_depth=2))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")


class OneShotTests(unittest.TestCase):
    def test_child_failure_is_a_stop_reason_not_an_exception(self):
        result = build_service().start(request(prompt="please fail"))
        self.assertEqual(result.stop_reason, team.STOP_ERROR)
        self.assertFalse(result.ok)
        self.assertIn("could not finish", result.error)

    def test_completed_value_is_returned_as_is(self):
        result = build_service().start(request(prompt="x"))
        self.assertEqual(result.value, "done: x")
        self.assertIsNone(result.error)


class ContinuableTests(unittest.TestCase):
    def test_spawn_queues_the_prompt_without_delivering_it(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        self.assertEqual([m["text"] for m in child.pending()], ["hello"])
        self.assertEqual(child.transcript, [])

    def test_pending_is_queued_minus_delivered(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        child.run_turn()
        self.assertEqual(child.pending(), [])

    def test_run_turn_delivers_queued_messages_fifo(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        child.send("second")
        child.run_turn()
        texts = [row["text"] for row in child.transcript if row["role"] == "user"]
        self.assertEqual(texts, ["hello", "second"])

    def test_run_turn_without_pending_work_returns_empty_text(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        child.run_turn()
        self.assertEqual(child.run_turn(), "")

    def test_interrupt_records_a_notice_without_closing_the_child(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        child.interrupt()
        self.assertFalse(child.closed)
        self.assertEqual(child.transcript[-1]["role"], "notice")

    def test_settle_releases_the_slot_and_writes_a_summary(self):
        service = build_service(max_active=1)
        child = service.spawn_continuable(request(provider="resumable"))
        self.assertEqual(service.active_slots(), 1)
        self.assertEqual(child.settle(), "It left no closing message.")
        self.assertEqual(service.active_slots(), 0)

    def test_a_settled_child_refuses_new_messages(self):
        child = build_service().spawn_continuable(request(provider="resumable"))
        child.settle()
        with self.assertRaises(team.SubagentError) as caught:
            child.send("too late")
        self.assertEqual(caught.exception.code, "CHILD_CLOSED")

    def test_provider_without_continuable_support_is_refused(self):
        with self.assertRaises(team.SubagentError) as caught:
            build_service().spawn_continuable(request(provider="spawn"))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")


class CapacityTests(unittest.TestCase):
    def test_capacity_is_refused_instead_of_queued(self):
        service = build_service(max_active=1)
        service.spawn_continuable(request(provider="resumable", prompt="a"))
        with self.assertRaises(team.SubagentError) as caught:
            service.spawn_continuable(request(provider="resumable", prompt="b"))
        self.assertEqual(caught.exception.code, "ACTIVATION_LIMIT_REACHED")
        self.assertEqual(service.active_slots(), 1)

    def test_a_failed_spawn_does_not_leak_a_slot(self):
        service = build_service(max_active=1)

        def broken_factory(_service, _request):
            raise RuntimeError("activation failed")

        service.register(team.Provider("broken", team.echo_handler,
                                       continuable=broken_factory))
        with self.assertRaises(RuntimeError):
            service.spawn_continuable(request(provider="broken"))
        self.assertEqual(service.active_slots(), 0)

    def test_one_shot_runs_do_not_consume_continuable_slots(self):
        service = build_service(max_active=0)
        for _ in range(3):
            self.assertTrue(service.start(request()).ok)
        self.assertEqual(service.active_slots(), 0)


# ---------------------------------------------------------------------------
# Agent team
# ---------------------------------------------------------------------------

class TaskBoardTests(unittest.TestCase):
    def board_with_two_tasks(self):
        board = team.TaskBoard()
        design = board.create("design")
        build = board.create("build", blocked_by=(design.id,))
        return board, design, build

    def test_new_tasks_start_pending_and_blocked(self):
        board, design, build = self.board_with_two_tasks()
        self.assertEqual(design.status, team.TASK_PENDING)
        self.assertFalse(board.ready(build.id))
        self.assertTrue(board.ready(design.id))

    def test_completing_a_blocker_unblocks_the_dependent(self):
        board, design, build = self.board_with_two_tasks()
        board.update(design.id, design.revision, status=team.TASK_COMPLETED)
        self.assertTrue(board.ready(build.id))

    def test_update_is_compare_and_set_on_revision(self):
        board, design, _build = self.board_with_two_tasks()
        updated = board.update(design.id, design.revision, status=team.TASK_COMPLETED)
        self.assertEqual(updated.revision, design.revision + 1)
        with self.assertRaises(team.SubagentError) as caught:
            board.update(design.id, design.revision, status=team.TASK_PENDING)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")

    def test_in_progress_requires_an_owner(self):
        board, design, _build = self.board_with_two_tasks()
        with self.assertRaises(team.SubagentError) as caught:
            board.update(design.id, design.revision, status=team.TASK_IN_PROGRESS)
        self.assertEqual(caught.exception.code, "OWNER_REQUIRED")

    def test_in_progress_with_an_owner_is_accepted(self):
        board, design, _build = self.board_with_two_tasks()
        updated = board.update(design.id, design.revision,
                               status=team.TASK_IN_PROGRESS, owner_id="session-2")
        self.assertEqual(updated.owner_id, "session-2")

    def test_unknown_status_is_refused(self):
        board, design, _build = self.board_with_two_tasks()
        with self.assertRaises(team.SubagentError) as caught:
            board.update(design.id, design.revision, status="paused")
        self.assertEqual(caught.exception.code, "INVALID_STATUS")

    def test_unknown_blocker_is_refused(self):
        board = team.TaskBoard()
        with self.assertRaises(team.SubagentError) as caught:
            board.create("orphan", blocked_by=("task-99",))
        self.assertEqual(caught.exception.code, "UNKNOWN_BLOCKER")

    def test_deleted_blocker_is_a_tombstone_not_a_target(self):
        board = team.TaskBoard()
        design = board.create("design")
        board.update(design.id, design.revision, status=team.TASK_DELETED)
        with self.assertRaises(team.SubagentError) as caught:
            board.create("build", blocked_by=(design.id,))
        self.assertEqual(caught.exception.code, "DELETED_BLOCKER")

    def test_self_block_is_refused(self):
        board = team.TaskBoard()
        first = board.create("a")
        second = board.create("b", blocked_by=(first.id,))
        with self.assertRaises(team.SubagentError) as caught:
            board.update(second.id, second.revision, blocked_by=(second.id,))
        self.assertEqual(caught.exception.code, "CYCLIC_BLOCK")

    def test_a_cycle_through_existing_tasks_is_refused(self):
        board = team.TaskBoard()
        first = board.create("a")
        second = board.create("b", blocked_by=(first.id,))
        with self.assertRaises(team.SubagentError) as caught:
            board.update(first.id, first.revision, blocked_by=(second.id,))
        self.assertEqual(caught.exception.code, "CYCLIC_BLOCK")

    def test_write_scopes_are_advisory_warnings(self):
        board = team.TaskBoard()
        first = board.create("a", write_scopes=("src/",))
        second = board.create("b", write_scopes=("src/",))
        board.update(first.id, first.revision, status=team.TASK_IN_PROGRESS,
                     owner_id="session-2")
        board.update(second.id, second.revision, status=team.TASK_IN_PROGRESS,
                     owner_id="session-3")
        warnings = board.write_scope_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("advisory, not locks", warnings[0])

    def test_no_warning_when_scopes_do_not_overlap(self):
        board = team.TaskBoard()
        first = board.create("a", write_scopes=("src/",))
        second = board.create("b", write_scopes=("docs/",))
        board.update(first.id, first.revision, status=team.TASK_IN_PROGRESS,
                     owner_id="session-2")
        board.update(second.id, second.revision, status=team.TASK_IN_PROGRESS,
                     owner_id="session-3")
        self.assertEqual(board.write_scope_warnings(), [])

    def test_unknown_task_update_is_refused(self):
        with self.assertRaises(team.SubagentError) as caught:
            team.TaskBoard().update("task-7", 1, status=team.TASK_COMPLETED)
        self.assertEqual(caught.exception.code, "UNKNOWN_TASK")


class RosterTests(unittest.TestCase):
    def test_members_start_provisioning_and_reach_a_terminal_phase(self):
        roster = team.Roster()
        member = roster.spawn("builder", provider="resumable", description="writes code")
        self.assertEqual(member.phase, "provisioning")
        self.assertEqual(roster.running(), [])
        member.activate()
        self.assertEqual(roster.running(), [member])
        other = roster.spawn("docs", provider="spawn")
        other.fail("provider crashed")
        self.assertEqual(roster.inactive(), [other])
        self.assertEqual(other.error, "provider crashed")

    def test_by_name_lookup_and_unknown_member(self):
        roster = team.Roster()
        roster.spawn("builder", provider="spawn")
        self.assertEqual(roster.by_name("builder").name, "builder")
        with self.assertRaises(team.SubagentError) as caught:
            roster.by_name("ghost")
        self.assertEqual(caught.exception.code, "UNKNOWN_MEMBER")


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.meta = team.WorkflowMeta(name="demo", description="a demo workflow")

    def run_workflow(self, script, **kwargs):
        run = team.start_workflow(self.service, self.meta, script, ROOT, **kwargs)
        result = run.execute()
        run.dispose()
        return result

    def test_start_requires_name_and_description(self):
        for meta in (team.WorkflowMeta(name="", description="x"),
                     team.WorkflowMeta(name="x", description="")):
            with self.assertRaises(team.WorkflowError) as caught:
                team.start_workflow(self.service, meta, lambda ctx: None, ROOT)
            self.assertEqual(caught.exception.code, "INVALID_META")
            self.assertTrue(caught.exception.fatal)

    def test_a_plain_script_completes_with_its_return_value(self):
        result = self.run_workflow(lambda ctx: "ok")
        self.assertEqual(result.stop_reason, team.STOP_COMPLETED)
        self.assertEqual(result.value, "ok")
        self.assertEqual(result.agents_started, 0)

    def test_agent_returns_the_child_value(self):
        result = self.run_workflow(lambda ctx: ctx.agent("first"))
        self.assertEqual(result.value, "done: first")
        self.assertEqual(result.agents_started, 1)

    def test_parallel_collects_values_in_order(self):
        result = self.run_workflow(
            lambda ctx: ctx.parallel(lambda: ctx.agent("a"), lambda: ctx.agent("b"))
        )
        self.assertEqual(result.value, ["done: a", "done: b"])
        self.assertEqual(result.agents_started, 2)

    def test_pipeline_threads_the_previous_value(self):
        result = self.run_workflow(
            lambda ctx: ctx.pipeline(lambda value: ctx.agent("one"),
                                     lambda value: ctx.agent("two"))
        )
        self.assertEqual(result.value, "done: two")

    def test_a_failed_child_maps_to_none_without_failing_the_run(self):
        result = self.run_workflow(
            lambda ctx: ctx.parallel(lambda: ctx.agent("ok"),
                                     lambda: ctx.agent("please fail"))
        )
        self.assertEqual(result.stop_reason, team.STOP_COMPLETED)
        self.assertEqual(result.value, ["done: ok", None])

    def test_output_schema_returns_the_structured_value(self):
        result = self.run_workflow(
            lambda ctx: ctx.agent("x", provider="deep", output_schema={"type": "object"})
        )
        self.assertEqual(result.value, {"summary": "done: x"})

    def test_exceeding_the_agent_ceiling_settles_as_error(self):
        result = self.run_workflow(
            lambda ctx: [ctx.agent("a"), ctx.agent("b"), ctx.agent("c")],
            max_total_agents=2,
        )
        self.assertEqual(result.stop_reason, team.STOP_ERROR)
        self.assertIn("more than 2 agents", result.error)

    def test_a_plain_script_error_settles_as_error_without_raising(self):
        def broken(_ctx):
            raise RuntimeError("script blew up")

        result = self.run_workflow(broken)
        self.assertEqual(result.stop_reason, team.STOP_ERROR)
        self.assertIn("script blew up", result.error)

    def test_cancellation_settles_as_cancelled(self):
        run = team.start_workflow(self.service, self.meta,
                                  lambda ctx: ctx.agent("slow"), ROOT)
        run.cancel("user pressed stop")
        result = run.execute()
        self.assertEqual(result.stop_reason, team.STOP_CANCELLED)
        self.assertIsNone(result.value)

    def test_execute_returns_a_result_instead_of_raising(self):
        run = team.start_workflow(self.service, self.meta,
                                  lambda ctx: ctx.agent("a"), ROOT,
                                  max_total_agents=0)
        result = run.execute()   # must not raise
        self.assertEqual(result.stop_reason, team.STOP_ERROR)

    def test_events_are_observations_without_control_handles(self):
        events = team.WorkflowEvents()
        captured = []
        events.on(captured.append)
        self.run_workflow(lambda ctx: ctx.agent("a"), events=events)
        kinds = [payload["kind"] for payload in captured]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "end")
        self.assertFalse(hasattr(captured[0]["run"], "cancel"))
        self.assertEqual(captured[0]["run"]["meta"], self.meta)
        self.assertTrue(captured[0]["run"]["id"].startswith("run-"))

    def test_each_listener_gets_its_own_payload_copy(self):
        events = team.WorkflowEvents()
        seen = []
        events.on(lambda payload: seen.append(payload))
        events.on(lambda payload: payload.update({"kind": "tampered"}))
        self.run_workflow(lambda ctx: None, events=events)
        self.assertEqual([p["kind"] for p in seen], ["start", "end"])

    def test_the_end_event_omits_the_result_value(self):
        events = team.WorkflowEvents()
        end_payloads = []
        events.on(lambda payload: end_payloads.append(payload)
                  if payload["kind"] == "end" else None)
        self.run_workflow(lambda ctx: "secret", events=events)
        self.assertEqual(end_payloads[0]["stop_reason"], team.STOP_COMPLETED)
        self.assertNotIn("value", end_payloads[0])


if __name__ == "__main__":
    unittest.main()