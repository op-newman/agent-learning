"""Offline lesson 7: a miniature Cordis-style plugin kernel.

This module models the parts of Cordis that shape how harness plugins are
written: the context as a service container, inject-based dependency waiting,
typed event dispatch modes, reversible registration, fiber lifecycle, child
plugins, and config-driven reload.

It is deliberately synchronous and single-threaded. The real framework awaits
async effects, dispatches ``parallel`` concurrently, and supports service
isolation; those differences are called out in the lesson text.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Mapping

MISSING = object()

PENDING = "PENDING"
LOADING = "LOADING"
ACTIVE = "ACTIVE"
FAILED = "FAILED"
UNLOADING = "UNLOADING"
DISPOSED = "DISPOSED"


class CordisError(Exception):
    """Framework error with a stable machine-readable code."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


class ValidationError(TypeError):
    """Raised when a plugin config fails schema validation."""

    def __init__(self, issues):
        super().__init__("; ".join(issues))
        self.issues = tuple(issues)


def field(kind, default=MISSING):
    """Declare one config field. Missing ``default`` makes it required."""

    return {"kind": kind, "default": default, "required": default is MISSING}


_TYPE_LABEL = {str: "string", int: "number", bool: "boolean"}


def _label(kind):
    return _TYPE_LABEL.get(kind, getattr(kind, "__name__", str(kind)))


def _matches(value, kind):
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


def validate_config(schema, raw):
    """Validate raw config, fill defaults, and reject unknown keys."""

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValidationError(["config must be a mapping"])
    issues = []
    resolved = {}
    for key, spec in schema.items():
        if key in raw:
            value = raw[key]
            if not _matches(value, spec["kind"]):
                issues.append(f"{key}: expected {_label(spec['kind'])}")
                continue
            resolved[key] = value
        elif spec["required"]:
            issues.append(f"{key}: required")
        else:
            resolved[key] = spec["default"]
    for key in raw:
        if key not in schema:
            issues.append(f"{key}: unknown key")
    if issues:
        raise ValidationError(issues)
    return resolved


@dataclass
class Listener:
    order: int
    fn: Callable


class EventBus:
    """Event dispatch with Cordis-style modes and inject bookkeeping."""

    def __init__(self):
        self._listeners = {}
        self._counter = 0
        self._registry = {}
        self._watchers = {}

    # -- listener registration -------------------------------------------

    def on(self, event, fn, prepend=False):
        self._counter += 1
        listener = Listener(self._counter, fn)
        bucket = self._listeners.setdefault(event, [])
        if prepend:
            bucket.insert(0, listener)
        else:
            bucket.append(listener)

        def off():
            if listener in bucket:
                bucket.remove(listener)
            if not bucket:
                self._listeners.pop(event, None)

        return off

    def listeners(self, event):
        return tuple(listener.fn for listener in self._listeners.get(event, ()))

    # -- dispatch modes ---------------------------------------------------

    def emit(self, event, *args):
        """Broadcast: every listener observes the event; results are ignored."""

        for fn in self.listeners(event):
            fn(*args)
        return None

    def bail(self, event, *args):
        """Stop at the first listener that returns a non-null value."""

        for fn in self.listeners(event):
            value = fn(*args)
            if value is not None and value is not False:
                return value
        return None

    def serial(self, event, *args):
        """Await listeners in order, stopping at the first decisive value."""

        for fn in self.listeners(event):
            value = fn(*args)
            if value is not None and value is not False:
                return value
        return None

    def parallel(self, event, *args):
        """Collect every listener result.

        The real framework awaits the listeners concurrently and this model
        preserves the result shape rather than the timing.
        """

        return tuple(fn(*args) for fn in self.listeners(event))

    def waterfall(self, event, value, fallback, *args):
        """Wrap the downstream value. A listener that skips ``next()`` short-circuits."""

        chain = self.listeners(event)

        def step(index):
            if index >= len(chain):
                return fallback()
            return chain[index](value, *args, lambda: step(index + 1))

        return step(0)

    # -- service registry -------------------------------------------------

    def register_service(self, key, context, value):
        self._registry[key] = (context, value)

    def unregister_service(self, key, context):
        entry = self._registry.get(key)
        if entry is not None and entry[0] is context:
            del self._registry[key]

    def has_service(self, key):
        return key in self._registry

    def lookup(self, key):
        """Resolve a service; only an active provider counts."""

        entry = self._registry.get(key)
        if entry is None:
            return None
        context, value = entry
        if context.fiber.state != ACTIVE:
            return None
        return value

    # -- inject watchers --------------------------------------------------

    def watch(self, key, context):
        self._watchers.setdefault(key, []).append(context)

    def unwatch(self, context):
        for bucket in self._watchers.values():
            if context in bucket:
                bucket.remove(context)

    def service_added(self, key):
        for context in list(self._watchers.get(key, ())):
            if context.fiber.state == PENDING and context.dependencies_ready():
                context.load()

    def service_removed(self, key):
        for context in list(self._watchers.get(key, ())):
            if context.fiber.state == ACTIVE and key in context.inject:
                context.unload()


class Fiber:
    """Lifecycle state, effects, and child fibers for one plugin instance."""

    def __init__(self, context):
        self.context = context
        self.state = PENDING
        self.effects = []
        self.children = []
        self.parent = None
        self.config = None

    def add_effect(self, label, disposer):
        if self.state in (UNLOADING, DISPOSED):
            raise CordisError("INACTIVE_EFFECT", "cannot create effect on inactive context")
        self.effects.append((label, disposer))
        return disposer

    def run_disposers(self):
        # Cleanup starts in reverse registration order.
        while self.effects:
            _, disposer = self.effects.pop()
            disposer()


class Service:
    """Base class for a plugin that publishes a service under a stable key."""

    inject = ()
    service_name = None
    Config = None
    plugin_name = None

    def __init__(self, ctx, name=None, config=None):
        self.ctx = ctx
        self.config = dict(config or {})
        resolved = name or self.service_name
        if resolved is None:
            raise CordisError("SERVICE_NAME_MISSING", "pass a service name to super()")
        ctx.provide(resolved, self)


class Context:
    """Service container, event surface, and plugin host."""

    def __init__(self, bus, name="root", parent=None):
        self.bus = bus
        self.name = name
        self.parent = parent
        self.fiber = Fiber(self)
        self.inject = ()
        self.spec = None
        self.raw_config = {}
        self.config = {}
        self._provided_keys = set()
        if parent is None:
            self.fiber.state = ACTIVE

    # -- services ---------------------------------------------------------

    def get(self, key):
        return self.bus.lookup(key)

    def dependencies_ready(self):
        return all(self.get(key) is not None for key in self.inject)

    def provide(self, key, value):
        if self.bus.has_service(key):
            raise CordisError("SERVICE_EXISTS", f"service {key!r} is already provided")
        self._provided_keys.add(key)
        self.bus.register_service(key, self, value)

        def remove():
            self.bus.unregister_service(key, self)
            self.bus.service_removed(key)

        self.fiber.add_effect(f"provide({key})", remove)
        self.bus.service_added(key)
        return value

    # -- events and effects ----------------------------------------------

    def on(self, event, fn, prepend=False):
        off = self.bus.on(event, fn, prepend=prepend)
        self.fiber.add_effect(f"on({event})", off)
        return off

    def effect(self, execute, label="effect"):
        if self.fiber.state in (UNLOADING, DISPOSED):
            raise CordisError("INACTIVE_EFFECT", "cannot create effect on inactive context")
        disposer = execute()
        self.fiber.add_effect(label, disposer)
        return disposer

    # -- plugin lifecycle -------------------------------------------------

    def plugin(self, plugin, config=None):
        spec = _as_spec(plugin)
        child = Context(self.bus, name=spec.name, parent=self)
        child.spec = spec
        child.inject = tuple(spec.inject)
        child.raw_config = dict(config or {})
        self.fiber.children.append(child)
        child.fiber.parent = self.fiber
        for key in child.inject:
            self.bus.watch(key, child)
        if child.dependencies_ready():
            child.load()
        return child

    def load(self):
        if self.spec is None:
            return self
        if not self.dependencies_ready():
            self.fiber.state = PENDING
            return self
        validated = validate_config(self.spec.config_schema or {}, self.raw_config)
        self.fiber.state = LOADING
        self.fiber.config = validated
        self.config = validated
        try:
            self.spec.apply(self, validated)
        except Exception:
            self.fiber.state = FAILED
            raise
        self.fiber.state = ACTIVE
        for key in self._provided_keys:
            self.bus.service_added(key)
        return self

    def unload(self):
        if self.fiber.state in (PENDING, DISPOSED):
            return self
        self.fiber.state = UNLOADING
        for child in list(self.fiber.children):
            child.dispose()
        self.fiber.children.clear()
        self.fiber.run_disposers()
        self.fiber.state = PENDING
        return self

    def dispose(self):
        self.unload()
        self.fiber.state = DISPOSED
        self.bus.unwatch(self)
        parent = self.fiber.parent
        if parent is not None and self in parent.children:
            parent.children.remove(self)
        self.fiber.parent = None
        return self

    def update_config(self, new_raw):
        if self.fiber.state == DISPOSED:
            raise CordisError("INACTIVE_EFFECT", "cannot update a disposed plugin")
        validated = validate_config(self.spec.config_schema or {}, new_raw)

        def restart():
            self.raw_config = dict(new_raw)
            self.unload()
            self.load()
            return "restarted"

        return self.bus.waterfall("internal/update", validated, restart)


@dataclass
class PluginSpec:
    name: str
    apply: Callable
    inject: tuple = ()
    config_schema: Mapping = dataclass_field(default_factory=dict)


def plugin(name=None, inject=(), config_schema=None):
    """Turn a plain function into a PluginSpec."""

    def decorate(fn):
        return PluginSpec(
            name=name or fn.__name__,
            apply=fn,
            inject=tuple(inject),
            config_schema=dict(config_schema or {}),
        )

    return decorate


def _as_spec(plugin):
    if isinstance(plugin, PluginSpec):
        return plugin
    if isinstance(plugin, type) and issubclass(plugin, Service):
        return PluginSpec(
            name=plugin.plugin_name or plugin.__name__,
            apply=lambda ctx, config: plugin(ctx, config=config),
            inject=tuple(getattr(plugin, "inject", ())),
            config_schema=dict(getattr(plugin, "Config", None) or {}),
        )
    if callable(plugin) and not isinstance(plugin, type):
        return PluginSpec(
            name=getattr(plugin, "__name__", "plugin"),
            apply=lambda ctx, config: plugin(ctx),
            inject=tuple(getattr(plugin, "inject", ())),
        )
    raise TypeError("a plugin must be a PluginSpec, a Service subclass, or a callable")


def main():
    print("== inject waits until the service exists ==")
    root = Context(EventBus(), name="root")
    started = []

    @plugin(name="metrics")
    def metrics_plugin(ctx, config):
        ctx.provide("metrics", {"records": []})

    @plugin(name="reporter", inject=("metrics",))
    def reporter(ctx, config):
        started.append("reporter")
        ctx.get("metrics")["records"].append("started")

    reporter_ctx = root.plugin(reporter)
    print("reporter before metrics:", reporter_ctx.fiber.state)
    root.plugin(metrics_plugin)
    print("reporter after metrics:", reporter_ctx.fiber.state)

    print("\n== event dispatch modes ==")
    bus = EventBus()
    log = []
    bus.on("demo", lambda value: log.append(("a", value)))
    bus.on("demo", lambda value: log.append(("b", value)))
    bus.emit("demo", 1)
    print("emit log:", log)
    bus.on("check", lambda value: None)
    bus.on("check", lambda value: "blocked" if value > 0 else None)
    bus.on("check", lambda value: "never reached")
    print("bail:", bus.bail("check", 1))

    print("\n== reversible registration ==")
    order = []

    def effect_that_disposes(name):
        def disposer():
            order.append(name)

        return lambda: disposer

    octx = Context(bus, name="effects", parent=root)
    octx.spec = PluginSpec("effects", lambda ctx, config: None)
    octx.fiber.state = ACTIVE
    octx.effect(effect_that_disposes("first"), "first")
    octx.effect(effect_that_disposes("second"), "second")
    octx.dispose()
    print("disposal order:", order)

    print("\n== service removal disposes and restores a dependent ==")
    events = []

    @plugin(name="provider")
    def provider(ctx, config):
        ctx.provide("thing", object())

    @plugin(name="consumer", inject=("thing",))
    def consumer(ctx, config):
        events.append("consumer active")

    root2 = Context(EventBus(), name="root2")
    provider_ctx = root2.plugin(provider)
    consumer_ctx = root2.plugin(consumer)
    print("consumer:", consumer_ctx.fiber.state, events)
    provider_ctx.dispose()
    print("consumer after provider dispose:", consumer_ctx.fiber.state)

    print("\n== config defaults, validation, and reload ==")
    schema = {"greeting": field(str, "Hello"), "maxRetries": field(int, 3)}

    @plugin(name="greeter", config_schema=schema)
    def greeter(ctx, config):
        print("  greeting:", config["greeting"], "maxRetries:", config["maxRetries"])

    greeter_ctx = root2.plugin(greeter, {"maxRetries": 5})
    greeter_ctx.update_config({"greeting": "Hi", "maxRetries": 7})
    try:
        greeter_ctx.update_config({"greeting": "Hi", "extra": True})
    except ValidationError as error:
        print("rejected:", error)


if __name__ == "__main__":
    main()