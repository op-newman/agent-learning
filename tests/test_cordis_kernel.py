"""Behavior checks for lesson 7; no model, network, or real filesystem is used."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import cordis_kernel as cordis


class EventDispatchTests(unittest.TestCase):
    def setUp(self):
        self.bus = cordis.EventBus()

    def test_emit_runs_every_listener_in_order_and_ignores_results(self):
        seen = []
        self.bus.on("demo", lambda value: seen.append(("a", value)))
        self.bus.on("demo", lambda value: seen.append(("b", value)))
        self.assertIsNone(self.bus.emit("demo", 1))
        self.assertEqual(seen, [("a", 1), ("b", 1)])

    def test_bail_stops_at_first_decisive_value(self):
        seen = []
        self.bus.on("check", lambda value: seen.append("a"))
        self.bus.on("check", lambda value: "blocked" if value else None)
        self.bus.on("check", lambda value: seen.append("c"))
        self.assertEqual(self.bus.bail("check", True), "blocked")
        self.assertEqual(seen, ["a"])

    def test_serial_stops_at_first_decisive_value(self):
        seen = []
        self.bus.on("setup", lambda: seen.append("a"))
        self.bus.on("setup", lambda: "ready")
        self.bus.on("setup", lambda: seen.append("c"))
        self.assertEqual(self.bus.serial("setup"), "ready")
        self.assertEqual(seen, ["a"])

    def test_parallel_collects_every_result_without_short_circuit(self):
        self.bus.on("fan", lambda: "a")
        self.bus.on("fan", lambda: None)
        self.bus.on("fan", lambda: "c")
        self.assertEqual(self.bus.parallel("fan"), ("a", None, "c"))

    def test_waterfall_wraps_the_fallback_value(self):
        self.bus.on("transform", lambda value, next_: "[" + next_() + "]")
        self.bus.on("transform", lambda value, next_: next_() + "!")
        result = self.bus.waterfall("transform", "x", lambda: "base")
        self.assertEqual(result, "[base!]")

    def test_waterfall_short_circuits_when_next_is_not_called(self):
        self.bus.on("transform", lambda value, next_: "replaced")
        self.bus.on("transform", lambda value, next_: "never reached")
        self.assertEqual(
            self.bus.waterfall("transform", "x", lambda: "base"), "replaced",
        )

    def test_prepend_listener_runs_before_earlier_registrations(self):
        order = []
        self.bus.on("demo", lambda: order.append("first"))
        self.bus.on("demo", lambda: order.append("prepended"), prepend=True)
        self.bus.emit("demo")
        self.assertEqual(order, ["prepended", "first"])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = cordis.Context(cordis.EventBus(), name="root")

    def test_plugin_waits_for_its_injected_service(self):
        started = []

        @cordis.plugin(name="metrics")
        def metrics_plugin(ctx, config):
            ctx.provide("metrics", {"records": []})

        @cordis.plugin(name="reporter", inject=("metrics",))
        def reporter(ctx, config):
            started.append("reporter")

        reporter_ctx = self.root.plugin(reporter)
        self.assertEqual(reporter_ctx.fiber.state, cordis.PENDING)
        self.assertEqual(started, [])
        self.root.plugin(metrics_plugin)
        self.assertEqual(reporter_ctx.fiber.state, cordis.ACTIVE)
        self.assertEqual(started, ["reporter"])

    def test_service_removal_unloads_and_restores_the_dependent(self):
        starts = []

        @cordis.plugin(name="provider")
        def provider(ctx, config):
            ctx.provide("thing", object())

        @cordis.plugin(name="consumer", inject=("thing",))
        def consumer(ctx, config):
            starts.append("consumer")

        provider_ctx = self.root.plugin(provider)
        consumer_ctx = self.root.plugin(consumer)
        self.assertEqual(consumer_ctx.fiber.state, cordis.ACTIVE)
        provider_ctx.dispose()
        self.assertEqual(consumer_ctx.fiber.state, cordis.PENDING)
        self.root.plugin(provider)
        self.assertEqual(consumer_ctx.fiber.state, cordis.ACTIVE)
        self.assertEqual(starts, ["consumer", "consumer"])

    def test_duplicate_service_registration_is_rejected(self):
        self.root.spec = cordis.PluginSpec("root-plugin", lambda ctx, config: None)
        self.root.fiber.state = cordis.ACTIVE
        self.root.provide("thing", 1)
        with self.assertRaises(cordis.CordisError) as error:
            self.root.provide("thing", 2)
        self.assertEqual(error.exception.code, "SERVICE_EXISTS")

    def test_service_subclass_publishes_under_its_name(self):
        class MetricsService(cordis.Service):
            inject = ()
            service_name = "metrics"
            plugin_name = "metrics-service"

        metrics_ctx = self.root.plugin(MetricsService)
        self.assertIsInstance(self.root.get("metrics"), MetricsService)
        self.assertEqual(metrics_ctx.fiber.state, cordis.ACTIVE)
        metrics_ctx.dispose()
        self.assertIsNone(self.root.get("metrics"))

    def test_dependent_is_not_visible_once_provider_unloads(self):
        @cordis.plugin(name="provider")
        def provider(ctx, config):
            ctx.provide("thing", "value")

        provider_ctx = self.root.plugin(provider)
        self.assertEqual(self.root.get("thing"), "value")
        provider_ctx.unload()
        self.assertIsNone(self.root.get("thing"))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = cordis.Context(cordis.EventBus(), name="root")

    def test_effects_run_in_reverse_registration_order(self):
        order = []

        def effect_that_disposes(name):
            def disposer():
                order.append(name)

            return lambda: disposer

        @cordis.plugin(name="effects")
        def effects(ctx, config):
            ctx.effect(effect_that_disposes("first"), "first")
            ctx.effect(effect_that_disposes("second"), "second")

        plugin_ctx = self.root.plugin(effects)
        plugin_ctx.dispose()
        self.assertEqual(order, ["second", "first"])

    def test_event_listener_is_removed_on_dispose(self):
        seen = []

        @cordis.plugin(name="listener")
        def listener(ctx, config):
            ctx.on("ping", lambda: seen.append("pong"))

        plugin_ctx = self.root.plugin(listener)
        self.root.bus.emit("ping")
        plugin_ctx.dispose()
        self.root.bus.emit("ping")
        self.assertEqual(seen, ["pong"])

    def test_disposing_a_parent_disposes_children(self):
        @cordis.plugin(name="parent")
        def parent(ctx, config):
            pass

        @cordis.plugin(name="child")
        def child(ctx, config):
            pass

        parent_ctx = self.root.plugin(parent)
        child_ctx = parent_ctx.plugin(child)
        parent_ctx.dispose()
        self.assertEqual(child_ctx.fiber.state, cordis.DISPOSED)

    def test_effect_on_disposed_context_is_rejected(self):
        @cordis.plugin(name="simple")
        def simple(ctx, config):
            pass

        plugin_ctx = self.root.plugin(simple)
        plugin_ctx.dispose()
        with self.assertRaises(cordis.CordisError) as error:
            plugin_ctx.effect(lambda: (lambda: None))
        self.assertEqual(error.exception.code, "INACTIVE_EFFECT")

    def test_unload_then_reload_reapplies_the_plugin(self):
        applies = []

        @cordis.plugin(name="counting")
        def counting(ctx, config):
            applies.append(len(applies) + 1)

        plugin_ctx = self.root.plugin(counting)
        plugin_ctx.unload()
        plugin_ctx.load()
        self.assertEqual(applies, [1, 2])
        self.assertEqual(plugin_ctx.fiber.state, cordis.ACTIVE)


class ConfigTests(unittest.TestCase):
    SCHEMA = {"greeting": cordis.field(str, "Hello"), "retries": cordis.field(int, 3)}

    def setUp(self):
        self.root = cordis.Context(cordis.EventBus(), name="root")
        self.configs = []

        @cordis.plugin(name="greeter", config_schema=self.SCHEMA)
        def greeter(ctx, config):
            self.configs.append(dict(config))

        self.greeter = greeter

    def test_config_defaults_are_filled(self):
        self.root.plugin(self.greeter, {})
        self.assertEqual(self.configs[0], {"greeting": "Hello", "retries": 3})

    def test_provided_values_override_defaults(self):
        self.root.plugin(self.greeter, {"retries": 9})
        self.assertEqual(self.configs[0], {"greeting": "Hello", "retries": 9})

    def test_unknown_key_and_wrong_type_are_rejected(self):
        with self.assertRaises(cordis.ValidationError):
            self.root.plugin(self.greeter, {"extra": True})
        with self.assertRaises(cordis.ValidationError):
            self.root.plugin(self.greeter, {"retries": "many"})

    def test_update_config_restarts_and_cleans_up_registrations(self):
        applies = []

        @cordis.plugin(name="greeter", config_schema=self.SCHEMA)
        def greeter(ctx, config):
            applies.append(dict(config))
            ctx.on("ping", lambda: None)

        plugin_ctx = self.root.plugin(greeter, {"retries": 1})
        self.assertEqual(len(self.root.bus.listeners("ping")), 1)
        result = plugin_ctx.update_config({"retries": 2})
        self.assertEqual(result, "restarted")
        self.assertEqual(applies[-1]["retries"], 2)
        # The old registration was disposed before the new one landed.
        self.assertEqual(len(self.root.bus.listeners("ping")), 1)

    def test_update_waterfall_can_veto_restart(self):
        plugin_ctx = self.root.plugin(self.greeter, {"retries": 1})
        self.root.on("internal/update", lambda config, next_: "vetoed")
        result = plugin_ctx.update_config({"retries": 2})
        self.assertEqual(result, "vetoed")
        self.assertEqual(self.configs, [{"greeting": "Hello", "retries": 1}])


if __name__ == "__main__":
    unittest.main()