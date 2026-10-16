"""Behavior checks for lesson 5; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import context_budget as context


def instruction(path, content, depth):
    return context.InstructionFile(path, content, depth)


class InstructionBudgetTests(unittest.TestCase):
    def test_full_broad_to_specific_chain_is_kept_when_it_fits(self):
        files = [
            instruction("AGENTS.md", "root rules", 1),
            instruction("$DSH_HOME/AGENTS.md", "global rules", 0),
            instruction("packages/api/AGENTS.md", "api rules", 3),
        ]
        rendered = context.render_instruction_chain(files, 1000)
        self.assertEqual(
            [file.path for file in rendered.included],
            ["$DSH_HOME/AGENTS.md", "AGENTS.md", "packages/api/AGENTS.md"],
        )
        self.assertEqual(rendered.omitted, ())
        self.assertLessEqual(rendered.used_bytes, 1000)

    def test_tight_budget_drops_broader_files_before_the_specific_file(self):
        files = [
            instruction("$DSH_HOME/AGENTS.md", "G" * 120, 0),
            instruction("AGENTS.md", "R" * 120, 1),
            instruction("packages/api/AGENTS.md", "API" * 30, 3),
        ]
        rendered = context.render_instruction_chain(files, 500)
        self.assertEqual([file.path for file in rendered.included], ["packages/api/AGENTS.md"])
        self.assertEqual(
            [file.path for file in rendered.omitted],
            ["$DSH_HOME/AGENTS.md", "AGENTS.md"],
        )
        self.assertLessEqual(rendered.used_bytes, 500)

    def test_specific_file_is_truncated_on_a_utf8_boundary(self):
        files = [
            instruction("AGENTS.md", "规则" * 120, 1),
        ]
        rendered = context.render_instruction_chain(files, 500)
        self.assertEqual(len(rendered.truncated), 1)
        self.assertLess(rendered.truncated[0]["included_bytes"], rendered.truncated[0]["original_bytes"])
        self.assertLessEqual(rendered.used_bytes, 500)
        self.assertTrue(rendered.text.endswith("<\\/system-reminder>"))
        rendered.text.encode("utf-8").decode("utf-8")

    def test_empty_instruction_chain_produces_no_message(self):
        rendered = context.render_instruction_chain([], 100)
        self.assertEqual(rendered.text, "")
        self.assertEqual(rendered.included, ())
        self.assertEqual(rendered.omitted, ())


class ContextAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.system = context.ContextBlock(
            "system", "system", "system rules", "system", required=True, order=0)
        self.request = context.ContextBlock(
            "request", "user", "the current request", "request", required=True, order=50)
        self.new_history = context.ContextBlock(
            "history-new", "assistant", "new detail", "history", priority=80, order=40)
        self.old_history = context.ContextBlock(
            "history-old", "assistant", "old detail", "history", priority=10, order=30)

    def test_required_system_and_current_request_survive_optional_pressure(self):
        selection = context.assemble_context(
            [
                self.system,
                self.old_history,
                context.ContextBlock(
                    "huge-reference", "user", "x" * 1000, "reference", priority=100, order=20),
                self.request,
            ],
            40,
        )
        self.assertEqual([block.key for block in selection.blocks], ["system", "request"])
        self.assertCountEqual([block.key for block in selection.omitted], ["huge-reference", "history-old"])
        self.assertLessEqual(selection.used_bytes, 40)

    def test_optional_blocks_are_selected_by_priority_then_rendered_in_order(self):
        selection = context.assemble_context(
            [self.system, self.old_history, self.new_history, self.request], 180)
        self.assertEqual(
            [block.key for block in selection.blocks],
            ["system", "history-old", "history-new", "request"],
        )

    def test_required_context_can_exceed_the_budget(self):
        with self.assertRaises(context.ContextError) as error:
            context.assemble_context([self.system, self.request], 5)
        self.assertEqual(error.exception.code, "REQUIRED_CONTEXT_TOO_LARGE")

    def test_duplicate_keys_are_rejected(self):
        duplicate = context.ContextBlock("system", "system", "again", "system")
        with self.assertRaises(context.ContextError) as error:
            context.assemble_context([self.system, duplicate], 100)
        self.assertEqual(error.exception.code, "DUPLICATE_BLOCK")


class SkillRegistryTests(unittest.TestCase):
    def test_catalog_lists_names_and_descriptions_without_loading_bodies(self):
        registry = context.SkillRegistry()
        registry.register(context.Skill("release-check", "Check release metadata.", "SECRET BODY"))
        catalog = registry.render_catalog()
        self.assertIn("release-check", catalog)
        self.assertIn("Check release metadata.", catalog)
        self.assertNotIn("SECRET BODY", catalog)
        self.assertEqual(registry.load_count, 0)

    def test_loading_returns_the_full_body_and_counts_the_lookup(self):
        registry = context.SkillRegistry()
        registry.register(context.Skill("release-check", "Check release metadata.", "SECRET BODY"))
        loaded = registry.load("release-check")
        self.assertIn("SECRET BODY", loaded)
        self.assertIn('<skill_content name="release-check">', loaded)
        self.assertEqual(registry.load_count, 1)

    def test_body_edits_do_not_change_catalog_digest(self):
        registry = context.SkillRegistry()
        registry.register(context.Skill("release-check", "Check release metadata.", "body one"))
        before = registry.catalog_digest()
        registry.replace_body("release-check", "body two")
        self.assertEqual(registry.catalog_digest(), before)
        self.assertIn("body two", registry.load("release-check"))

    def test_model_invisible_skills_are_absent_from_the_catalog(self):
        registry = context.SkillRegistry()
        registry.register(context.Skill(
            "user-only", "Only the user can invoke this.", "body",
            model_invocable=False, user_invocable=True,
        ))
        self.assertNotIn("user-only", registry.render_catalog())
        with self.assertRaises(context.ContextError) as error:
            registry.load("user-only")
        self.assertEqual(error.exception.code, "SKILL_NOT_MODEL_INVOCABLE")


class CompactionTests(unittest.TestCase):
    def make_history(self):
        return (
            context.user_message(0, "First request"),
            context.assistant_tool_call(1, "call_1"),
            context.tool_result(2, "call_1", "first result"),
            context.assistant_tool_call(3, "call_2"),
            context.tool_result(4, "call_2", "second result"),
            context.user_message(5, "Second request"),
        )

    def test_compaction_moves_the_boundary_left_to_keep_a_tool_pair_together(self):
        calls = []
        result = context.compact_messages(
            self.make_history(), 2,
            lambda selected: calls.append(selected) or "summary",
        )
        self.assertEqual([message.seq for message in result.selected], [0, 1, 2])
        self.assertEqual([message.seq for message in result.retained], [3, 4, 5])
        self.assertEqual(len(calls), 1)

    def test_compaction_is_a_projection_over_an_unchanged_source(self):
        history = self.make_history()
        result = context.compact_messages(history, 2, lambda selected: "summary")
        visible = result.visible_messages()
        self.assertEqual(history, result.source)
        self.assertEqual([message.seq for message in visible], [3, 3, 4, 5])
        self.assertEqual(visible[0].role, "user")
        self.assertIn("<compacted-summary>", visible[0].content)
        self.assertNotIn("First request", "\n".join(message.content for message in visible))

    def test_compaction_events_are_recorded_but_only_the_replacement_is_model_visible(self):
        result = context.compact_messages(self.make_history(), 2, lambda selected: "summary")
        event_types = [event["type"] for event in result.recorded_events]
        self.assertEqual(
            event_types,
            ["compaction/start", "compaction/summary", "user/message", "compaction/end"],
        )
        self.assertEqual(
            [event_type for event_type in event_types
             if event_type in context.MODEL_VISIBLE_COMPACTION_TYPES],
            ["user/message"],
        )

    def test_no_safe_prefix_is_reported_instead_of_splitting_a_call(self):
        history = (
            context.assistant_tool_call(0, "call_1"),
            context.tool_result(1, "call_1", "result"),
        )
        self.assertIsNone(context.compact_messages(history, 1, lambda selected: "summary"))

    def test_an_empty_summary_is_rejected(self):
        with self.assertRaises(context.ContextError) as error:
            context.compact_messages(self.make_history(), 2, lambda selected: "  ")
        self.assertEqual(error.exception.code, "EMPTY_SUMMARY")


if __name__ == "__main__":
    unittest.main()