"""Behavior checks for lesson 8; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import assembly_layers as assembly


def base_profile(name="base", bundles=None, patch=()):
    return assembly.Profile(name, bundles or ["@deepseek-ai/dsh-base"], patch)


class LayerOrderTests(unittest.TestCase):
    def test_layers_are_applied_in_documented_order(self):
        tree = assembly.assemble(
            assembly.web_profile(),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
            home_patch=[{"id": "tools", "config": {"a": 1}}],
            overlays=[[{"id": "tools", "config": {"a": 2}}]],
        )
        self.assertEqual(
            tree.layers,
            [
                "@deepseek-ai/dsh-base#0",
                "@deepseek-ai/dsh-web-app#0",
                "profile:web",
                assembly.HOME_LAYER,
                "--patch[0]",
            ],
        )

    def test_last_write_wins_per_row(self):
        home = [{"id": "agent-default-model",
                 "config": {"provider": "deepseek-official", "model": "home"}}]
        overlay = [{"id": "agent-default-model", "config": {"model": "overlay"}}]
        tree = assembly.assemble(assembly.web_profile(), assembly.DEFAULT_REGISTRY,
                                 platform="linux", home_patch=home, overlays=[overlay])
        row = tree.get("agent-default-model")
        self.assertEqual(row.config, {"model": "overlay"})
        self.assertEqual(row.source, "--patch[0]")

    def test_overlays_are_applied_in_argument_order(self):
        first = [{"id": "tools", "config": {"order": "first"}}]
        second = [{"id": "tools", "config": {"order": "second"}}]
        tree = assembly.assemble(base_profile(), assembly.DEFAULT_REGISTRY,
                                 platform="linux", overlays=[first, second])
        self.assertEqual(tree.get("tools").config, {"order": "second"})

    def test_bundle_order_decides_the_final_row(self):
        overwriter = assembly.Bundle("late", [[
            {"id": "agent-default-model", "config": {"model": "from-late-bundle"}},
        ]])
        registry = assembly.Registry(
            [assembly.BASE_BUNDLE, overwriter]
        )
        tree = assembly.assemble(base_profile(bundles=["@deepseek-ai/dsh-base", "late"]),
                                 registry, platform="linux")
        self.assertEqual(tree.get("agent-default-model").config, {"model": "from-late-bundle"})

    def test_home_patch_is_skipped_when_absent(self):
        tree = assembly.assemble(base_profile(), assembly.DEFAULT_REGISTRY, platform="linux")
        self.assertNotIn(assembly.HOME_LAYER, tree.layers)


class PatchSemanticsTests(unittest.TestCase):
    def test_config_patch_replaces_instead_of_merging(self):
        tree = assembly.assemble(
            base_profile(patch=[{"id": "agent-default-model",
                                 "config": {"model": "deepseek-pro"}}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        # The provider field is gone: the patch did not deep-merge.
        self.assertEqual(tree.get("agent-default-model").config, {"model": "deepseek-pro"})

    def test_restating_every_field_keeps_the_merged_result(self):
        tree = assembly.assemble(
            base_profile(patch=[{"id": "agent-default-model",
                                 "config": {"provider": "deepseek-official",
                                            "model": "deepseek-pro"}}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        self.assertEqual(tree.get("agent-default-model").config,
                         {"provider": "deepseek-official", "model": "deepseek-pro"})

    def test_patching_an_unknown_id_is_rejected(self):
        with self.assertRaises(assembly.AssemblyError) as error:
            assembly.assemble(base_profile(patch=[{"id": "does-not-exist", "config": {}}]),
                              assembly.DEFAULT_REGISTRY, platform="linux")
        self.assertEqual(error.exception.code, "UNKNOWN_ENTRY")

    def test_inserting_a_duplicate_id_is_rejected(self):
        with self.assertRaises(assembly.AssemblyError) as error:
            assembly.assemble(
                base_profile(patch=[{"insert": [{"id": "tools", "name": "again"}]}]),
                assembly.DEFAULT_REGISTRY,
                platform="linux",
            )
        self.assertEqual(error.exception.code, "DUPLICATE_ENTRY")

    def test_missing_bundle_is_reported(self):
        with self.assertRaises(assembly.AssemblyError) as error:
            assembly.assemble(base_profile(bundles=["not-installed"]),
                              assembly.DEFAULT_REGISTRY, platform="linux")
        self.assertEqual(error.exception.code, "BUNDLE_NOT_FOUND")

    def test_insert_without_name_is_rejected(self):
        with self.assertRaises(assembly.AssemblyError) as error:
            assembly.assemble(
                base_profile(patch=[{"insert": [{"id": "x"}]}]),
                assembly.DEFAULT_REGISTRY,
                platform="linux",
            )
        self.assertEqual(error.exception.code, "INVALID_PATCH")

    def test_entry_without_insert_or_id_is_rejected(self):
        with self.assertRaises(assembly.AssemblyError) as error:
            assembly.assemble(base_profile(patch=[{"name": "x"}]),
                              assembly.DEFAULT_REGISTRY, platform="linux")
        self.assertEqual(error.exception.code, "INVALID_PATCH")

    def test_history_records_every_layer_that_touched_the_row(self):
        tree = assembly.assemble(
            base_profile(patch=[{"id": "tools", "config": {"a": 1}}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
            home_patch=[{"id": "tools", "config": {"a": 2}}],
        )
        self.assertEqual(tree.get("tools").history,
                         ["@deepseek-ai/dsh-base#0", "profile:base", assembly.HOME_LAYER])


class PlatformGateTests(unittest.TestCase):
    def test_only_the_posix_shell_stack_is_active_on_linux(self):
        tree = assembly.assemble(assembly.web_profile(), assembly.DEFAULT_REGISTRY,
                                 platform="linux")
        self.assertTrue(tree.get("bash-sandbox").active)
        self.assertFalse(tree.get("pwsh-sandbox").active)
        self.assertEqual(tree.get("pwsh-sandbox").disabled_reason, assembly.OFF_PLATFORM)

    def test_only_the_windows_shell_stack_is_active_on_win32(self):
        tree = assembly.assemble(assembly.web_profile(), assembly.DEFAULT_REGISTRY,
                                 platform="win32")
        self.assertTrue(tree.get("pwsh-sandbox").active)
        self.assertFalse(tree.get("bash-sandbox").active)

    def test_a_single_shell_stack_validates_cleanly(self):
        for platform in ("linux", "win32"):
            tree = assembly.assemble(assembly.web_profile(), assembly.DEFAULT_REGISTRY,
                                     platform=platform)
            self.assertTrue(assembly.validate(tree).ok, platform)

    def test_committing_disabled_false_clears_the_gate_and_breaks_the_stack(self):
        tree = assembly.assemble(
            base_profile(patch=[{"id": "pwsh-sandbox", "disabled": False},
                                {"id": "tool-pwsh", "disabled": False}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        report = assembly.validate(tree)
        self.assertFalse(report.ok)
        self.assertEqual(report.problems[0].code, "DUPLICATE_SERVICE")

    def test_explicit_disable_turns_a_row_off_on_its_own_platform(self):
        tree = assembly.assemble(
            base_profile(patch=[{"id": "bash-sandbox", "disabled": True}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        row = tree.get("bash-sandbox")
        self.assertFalse(row.active)
        self.assertEqual(row.disabled_reason, assembly.OFF_EXPLICIT)


class ValidationTests(unittest.TestCase):
    def test_a_second_filesystem_provider_is_a_problem(self):
        tree = assembly.assemble(
            base_profile(patch=[{"insert": [{"id": "fs-local", "name": "fs-local",
                                             "provides": ["fs"]}]}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        report = assembly.validate(tree)
        self.assertFalse(report.ok)
        self.assertEqual(report.problems[0].code, "DUPLICATE_SERVICE")

    def test_missing_service_is_a_warning_not_a_problem(self):
        registry = assembly.Registry([assembly.Bundle("empty", [[]])])
        tree = assembly.assemble(
            assembly.Profile("empty", ["empty"],
                             [{"insert": [{"id": "needs-shell", "name": "needs-shell",
                                           "requires": ["shell"]}]}]),
            registry,
            platform="linux",
        )
        report = assembly.validate(tree)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("needs-shell", report.warnings[0])

    def test_disabled_rows_do_not_count_as_providers(self):
        tree = assembly.assemble(
            base_profile(patch=[{"insert": [{"id": "fs-local", "name": "fs-local",
                                             "provides": ["fs"], "disabled": True}]}]),
            assembly.DEFAULT_REGISTRY,
            platform="linux",
        )
        self.assertTrue(assembly.validate(tree).ok)


class StandaloneTreeTests(unittest.TestCase):
    def test_sdk_minimal_does_not_inherit_base_rows(self):
        tree = assembly.assemble(
            assembly.Profile("sdk-minimal", ["@deepseek-ai/dsh-sdk-minimal"], []),
            assembly.DEFAULT_REGISTRY,
            platform="win32",
        )
        self.assertNotIn("tool-read", tree.rows)
        self.assertNotIn("fs-sandbox", tree.rows)
        self.assertIn("sdk-jsonrpc-server", tree.rows)

    def test_sdk_minimal_still_offers_exactly_one_shell(self):
        for platform in ("linux", "win32"):
            tree = assembly.assemble(
                assembly.Profile("sdk-minimal", ["@deepseek-ai/dsh-sdk-minimal"], []),
                assembly.DEFAULT_REGISTRY,
                platform=platform,
            )
            report = assembly.validate(tree)
            self.assertTrue(report.ok, platform)
            active = [row.id for row in tree.rows.values() if row.active and row.requires]
            self.assertEqual(len(active), 1, platform)


class DumpTests(unittest.TestCase):
    def test_dump_config_annotates_source_layers_and_marks_off_rows(self):
        tree = assembly.assemble(assembly.web_profile(), assembly.DEFAULT_REGISTRY,
                                 platform="win32")
        text = assembly.dump_config(tree)
        self.assertIn("plan-mode   (from @deepseek-ai/dsh-web-app#0)", text)
        self.assertIn("bash-sandbox", text)
        self.assertIn("[off]", text)


if __name__ == "__main__":
    unittest.main()