"""Offline lesson 9: delegation, agent teams, and workflow runs.

This module models the three cooperating seams DeepSeek Harness uses to spread
work out beyond a single loop:

* the **subagent seam** (``ctx.subagents``) -- a registry of named providers,
  capability flags checked before a run starts, a delegation-depth cap,
  one-shot children, and continuable children with a durable inbox;
* the **agent team** layer -- a roster, a durable queue-minus-delivered mailbox,
  and a shared task DAG whose ``revision`` acts as compare-and-set; and
* the **workflow engine** -- a script that calls ``agent()`` under a total-child
  ceiling, with a closed ``stopReason`` and observe-only events.

Everything here is deterministic, in memory, and offline. It demonstrates
protocol rules, not real concurrency or real model behaviour.
"""

from dataclasses import dataclass
from typing import Callable, Iterable

CAPABILITY_NAMES = (
    "agentOptions",
    "outputSchema",
    "depthLimit",
    "toolFilter",
    "persona",
)

# Optional request field -> the capability flag it requires.
CAPABILITY_FOR_FIELD = {
    "agent_options": "agentOptions",
    "output_schema": "outputSchema",
    "max_depth": "depthLimit",
    "tool_filter": "toolFilter",
    "persona": "persona",
}

TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_COMPLETED = "completed"
TASK_DELETED = "deleted"

STOP_COMPLETED = "completed"
STOP_CANCELLED = "cancelled"
STOP_ERROR = "error"


class SubagentError(Exception):
    """A delegation failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class WorkflowError(Exception):
    """A workflow-hook misuse. ``fatal`` marks errors combinators must re-raise."""

    def __init__(self, code: str, message: str | None = None, fatal: bool = True):
        super().__init__(message or code)
        self.code = code
        self.fatal = fatal


class ChildFailure(Exception):
    """A child run that settled without completing."""


# ---------------------------------------------------------------------------
# Subagent seam
# ---------------------------------------------------------------------------

@dataclass
class AgentHandle:
    """The durable identity a provider derives lineage and depth from."""

    session_id: str
    depth: int = 0
    name: str = "root"


@dataclass
class StartRequest:
    provider: str
    prompt: str
    parent: AgentHandle
    agent_options: dict | None = None
    output_schema: dict | None = None
    max_depth: int | None = None
    tool_filter: tuple | None = None
    persona: str | None = None


@dataclass
class SubagentResult:
    value: object = None
    stop_reason: str = STOP_COMPLETED
    error: str | None = None
    structured: dict | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason == STOP_COMPLETED


@dataclass
class Provider:
    """One named backend. ``handler`` runs a one-shot child; ``continuable``
    returns a :class:`ContinuableChild` when the provider supports them."""

    name: str
    handler: Callable
    capabilities: frozenset = frozenset()
    continuable: Callable | None = None


class ContinuableChild:
    """A durable child session with at most one live in-process activation.

    The inbox is the only queue. A message sits in ``inbox`` until it is
    delivered into the transcript, and ``pending()`` is exactly the queued set
    minus the delivered set -- which is what a crash-recovery mailbox reads.
    """

    _counter = 0

    def __init__(self, service, request: StartRequest, handler: Callable):
        ContinuableChild._counter += 1
        self.service = service
        self.id = f"session-{ContinuableChild._counter}"
        self.name = request.provider + "-child"
        self.depth = request.parent.depth + 1
        self.handler = handler
        self.inbox = []
        self.transcript = []
        self.closed = False
        self.settled_summary = None
        self._message_counter = 0

    # -- mailbox ----------------------------------------------------------

    def send(self, text: str, sender: str = "lead") -> str:
        """Queue a message. Delivery happens at the next turn, not now."""

        if self.closed:
            raise SubagentError("CHILD_CLOSED", f"{self.id} is no longer live")
        self._message_counter += 1
        message = {"id": f"msg-{self._message_counter}", "text": text, "sender": sender}
        # Queue only. Delivery happens at the next turn, which is what makes
        # queued-minus-delivered a valid crash-recovery mailbox.
        self.inbox.append(message)
        return message["id"]

    def pending(self) -> list:
        """Queued-minus-delivered: the durable mailbox a recovery would replay."""

        return list(self.inbox)

    def _deliver(self) -> list:
        delivered = []
        while self.inbox:
            message = self.inbox.pop(0)
            self.transcript.append(
                {"role": "user", "sender": message["sender"], "text": message["text"]}
            )
            delivered.append(message["id"])
        return delivered

    # -- turns ------------------------------------------------------------

    def run_turn(self) -> str:
        """Run one FIFO turn, delivering whatever is queued first."""

        if self.closed:
            raise SubagentError("CHILD_CLOSED", f"{self.id} is no longer live")
        delivered = self._deliver()
        if not delivered:
            return ""
        output = self.handler(self.transcript)
        self.transcript.append({"role": "assistant", "text": output})
        return output

    def interrupt(self) -> None:
        """Cancel the current turn without destroying the child."""

        self.transcript.append({"role": "notice", "text": "turn interrupted"})

    def settle(self) -> str:
        """Close the activation and release the capacity slot."""

        self.closed = True
        self.service.release_slot()
        self.settled_summary = "It left no closing message."
        return self.settled_summary


class SubagentService:
    """Named provider registry plus depth and activation-capacity accounting."""

    def __init__(self, max_depth: int = 1, max_active_subagents: int = 8):
        self.providers = {}
        self.max_depth = max_depth
        self.max_active_subagents = max_active_subagents
        self._active = 0

    def register(self, provider: Provider) -> None:
        self.providers[provider.name] = provider

    def provider(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError:
            raise SubagentError("UNKNOWN_PROVIDER", f"no provider named {name!r}") from None

    # -- capacity ---------------------------------------------------------

    def active_slots(self) -> int:
        return self._active

    def reserve_slot(self) -> None:
        if self._active >= self.max_active_subagents:
            raise SubagentError(
                "ACTIVATION_LIMIT_REACHED",
                f"all {self.max_active_subagents} continuable slots are in use",
            )
        self._active += 1

    def release_slot(self) -> None:
        if self._active:
            self._active -= 1

    # -- validation -------------------------------------------------------

    def _check_capabilities(self, provider: Provider, request: StartRequest) -> None:
        for attribute, flag in CAPABILITY_FOR_FIELD.items():
            if getattr(request, attribute) is None:
                continue
            if flag not in provider.capabilities:
                raise SubagentError(
                    "UNSUPPORTED_CAPABILITY",
                    f"provider {provider.name!r} does not support {flag}",
                )

    def _child_depth(self, request: StartRequest) -> int:
        depth = request.parent.depth + 1
        ceiling = request.max_depth if request.max_depth is not None else self.max_depth
        if depth > ceiling:
            raise SubagentError(
                "DEPTH_LIMIT_EXCEEDED",
                f"depth {depth} exceeds the maximum of {ceiling}",
            )
        return depth

    # -- one-shot start ---------------------------------------------------

    def start(self, request: StartRequest) -> SubagentResult:
        provider = self.provider(request.provider)
        self._check_capabilities(provider, request)
        self._child_depth(request)
        try:
            value = provider.handler(request.prompt)
        except ChildFailure as error:
            return SubagentResult(value=None, stop_reason=STOP_ERROR, error=str(error))
        if request.output_schema is not None:
            if not isinstance(value, dict):
                raise SubagentError(
                    "SCHEMA_MISMATCH", "output_schema requires a mapping result"
                )
            return SubagentResult(value=None, structured=value, stop_reason=STOP_COMPLETED)
        return SubagentResult(value=value, stop_reason=STOP_COMPLETED)

    # -- continuable start ------------------------------------------------

    def spawn_continuable(self, request: StartRequest) -> ContinuableChild:
        provider = self.provider(request.provider)
        if provider.continuable is None:
            raise SubagentError(
                "UNSUPPORTED_CAPABILITY",
                f"provider {provider.name!r} cannot host a continuable child",
            )
        self._check_capabilities(provider, request)
        self._child_depth(request)
        self.reserve_slot()
        try:
            child = provider.continuable(self, request)
        except Exception:
            self.release_slot()
            raise
        child.send(request.prompt, sender="lead")
        return child


# ---------------------------------------------------------------------------
# Agent team: roster, mailbox, task DAG
# ---------------------------------------------------------------------------

@dataclass
class TeamMember:
    id: str
    name: str
    description: str
    provider: str
    context: str          # "fresh" or "fork"
    phase: str = "provisioning"   # provisioning -> active | failed
    error: str | None = None

    def activate(self) -> None:
        self.phase = "active"

    def fail(self, error: str) -> None:
        self.phase = "failed"
        self.error = error


@dataclass
class TeamTask:
    id: str
    revision: int
    subject: str
    description: str = ""
    status: str = TASK_PENDING
    owner_id: str | None = None
    blocked_by: tuple = ()
    write_scopes: tuple = ()


class TaskBoard:
    """A shared DAG. Every mutation bumps ``revision`` (compare-and-set)."""

    def __init__(self):
        self.tasks = {}
        self._counter = 0

    def create(self, subject: str, description: str = "",
               blocked_by: Iterable = (), write_scopes: Iterable = ()) -> TeamTask:
        self._counter += 1
        task = TeamTask(
            id=f"task-{self._counter}",
            revision=1,
            subject=subject,
            description=description,
            blocked_by=tuple(blocked_by),
            write_scopes=tuple(write_scopes),
        )
        self._check_edges(task, replacing=None)
        self.tasks[task.id] = task
        return task

    def update(self, task_id: str, revision: int, **changes) -> TeamTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise SubagentError("UNKNOWN_TASK", f"no task {task_id!r}")
        if revision != task.revision:
            raise SubagentError(
                "REVISION_CONFLICT",
                f"{task_id} is at revision {task.revision}, not {revision}",
            )
        candidate = TeamTask(
            id=task.id,
            revision=task.revision,
            subject=changes.get("subject", task.subject),
            description=changes.get("description", task.description),
            status=changes.get("status", task.status),
            owner_id=changes.get("owner_id", task.owner_id),
            blocked_by=tuple(changes.get("blocked_by", task.blocked_by)),
            write_scopes=tuple(changes.get("write_scopes", task.write_scopes)),
        )
        if candidate.status not in (TASK_PENDING, TASK_IN_PROGRESS,
                                    TASK_COMPLETED, TASK_DELETED):
            raise SubagentError("INVALID_STATUS", f"unknown status {candidate.status!r}")
        if candidate.status == TASK_IN_PROGRESS and candidate.owner_id is None:
            raise SubagentError("OWNER_REQUIRED", "in_progress requires an owner")
        self._check_edges(candidate, replacing=task)
        candidate.revision = task.revision + 1
        self.tasks[task_id] = candidate
        return candidate

    def _check_edges(self, task: TeamTask, replacing: TeamTask | None) -> None:
        for blocker in task.blocked_by:
            if blocker == task.id:
                raise SubagentError("CYCLIC_BLOCK", f"{task.id} blocks itself")
            target = self.tasks.get(blocker)
            if target is None:
                raise SubagentError("UNKNOWN_BLOCKER", f"no task {blocker!r} to block on")
            if target.status == TASK_DELETED:
                raise SubagentError("DELETED_BLOCKER", f"{blocker} is a tombstone")
        # Walk the graph with this task's edges substituted in, and refuse a cycle.
        edges = {task_id: list(row.blocked_by) for task_id, row in self.tasks.items()}
        if replacing is not None:
            edges[replacing.id] = list(task.blocked_by)
        elif task.id not in edges:
            edges[task.id] = list(task.blocked_by)
        self._assert_acyclic(task.id, edges)

    def _assert_acyclic(self, start: str, edges: dict) -> None:
        seen = set()

        def walk(node, trail):
            if node in trail:
                raise SubagentError("CYCLIC_BLOCK", "blocked_by edges form a cycle")
            if node in seen:
                return
            seen.add(node)
            for nxt in edges.get(node, ()):
                walk(nxt, trail | {node})

        walk(start, set())

    def ready(self, task_id: str) -> bool:
        task = self.tasks[task_id]
        if task.status not in (TASK_PENDING, TASK_IN_PROGRESS):
            return False
        return all(self.tasks[b].status == TASK_COMPLETED for b in task.blocked_by)

    def write_scope_warnings(self) -> list:
        """Advisory overlap warnings for concurrently in-progress tasks."""

        live = [t for t in self.tasks.values() if t.status == TASK_IN_PROGRESS]
        warnings = []
        for index, first in enumerate(live):
            for second in live[index + 1:]:
                if set(first.write_scopes) & set(second.write_scopes):
                    warnings.append(
                        f"{first.id} and {second.id} share write scopes; "
                        "writeScopes are advisory, not locks"
                    )
        return warnings


class Roster:
    """Ordered teammates. ``provisioning`` always reaches ``active`` or ``failed``."""

    def __init__(self):
        self.members = []

    def spawn(self, name: str, provider: str, description: str = "",
              context: str = "fresh") -> TeamMember:
        member = TeamMember(
            id=f"session-{len(self.members) + 2}",
            name=name,
            description=description,
            provider=provider,
            context=context,
        )
        self.members.append(member)
        return member

    def by_name(self, name: str) -> TeamMember:
        for member in self.members:
            if member.name == name:
                return member
        raise SubagentError("UNKNOWN_MEMBER", f"no teammate named {name!r}")

    def running(self) -> list:
        return [m for m in self.members if m.phase == "active"]

    def inactive(self) -> list:
        return [m for m in self.members if m.phase != "active"]


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

@dataclass
class WorkflowMeta:
    name: str
    description: str
    when_to_use: str | None = None
    phases: tuple = ()


@dataclass
class WorkflowResult:
    value: object = None
    stop_reason: str = STOP_COMPLETED
    error: str | None = None
    agents_started: int = 0


class _WorkflowContext:
    """The globals a script sees. Only ``agent``/``parallel`` reach the engine."""

    def __init__(self, run):
        self.run = run

    def agent(self, prompt: str, provider: str | None = None,
              output_schema: dict | None = None) -> object:
        return self.run.agent(prompt, provider=provider, output_schema=output_schema)

    def parallel(self, *thunks) -> list:
        return self.run.parallel(*thunks)

    def pipeline(self, *thunks) -> object:
        return self.run.pipeline(*thunks)


class WorkflowRun:
    """A live workflow. ``result`` never raises; the caller must ``dispose``."""

    _counter = 0

    def __init__(self, service: SubagentService, meta: WorkflowMeta, script: Callable,
                 parent: AgentHandle, args=None, max_total_agents: int = 8,
                 events: "WorkflowEvents | None" = None):
        WorkflowRun._counter += 1
        self.id = f"run-{WorkflowRun._counter}"
        self.meta = meta
        self.script = script
        self.parent = parent
        self.args = args
        self.max_total_agents = max_total_agents
        self.events = events
        self.service = service
        self.agents_started = 0
        self.cancelled = False
        self.cancel_reason = None
        self.disposed = False

    # -- observation ------------------------------------------------------

    def _emit(self, kind: str, **extra) -> None:
        if self.events is None:
            return
        self.events.emit(kind, self, **extra)

    # -- script hooks -----------------------------------------------------

    def agent(self, prompt: str, provider: str | None = None,
              output_schema: dict | None = None) -> object:
        if self.cancelled:
            raise WorkflowError("CANCELLED", "run was cancelled", fatal=True)
        self.agents_started += 1
        if self.agents_started > self.max_total_agents:
            raise WorkflowError(
                "AGENT_LIMIT_EXCEEDED",
                f"run accepted more than {self.max_total_agents} agents",
                fatal=True,
            )
        request = StartRequest(
            provider=provider or "spawn",
            prompt=prompt,
            parent=self.parent,
            output_schema=output_schema,
        )
        self._emit("agent-start", prompt=prompt)
        result = self.service.start(request)
        self._emit("agent-end", ok=result.ok)
        if not result.ok:
            # A non-completed sub-run maps to per-item ``None``; it is not fatal.
            return None
        return result.structured if output_schema is not None else result.value

    def parallel(self, *thunks) -> list:
        self._emit("phase", name="parallel")
        outcomes = []
        for thunk in thunks:
            try:
                outcomes.append(thunk())
            except WorkflowError as error:
                if error.fatal:
                    raise
                outcomes.append(None)
        return outcomes

    def pipeline(self, *thunks) -> object:
        value = None
        for thunk in thunks:
            value = thunk(value)
        return value

    # -- lifecycle --------------------------------------------------------

    def cancel(self, reason: str = "cancelled") -> None:
        self.cancelled = True
        self.cancel_reason = reason

    def execute(self) -> WorkflowResult:
        """Run the script. The returned result never raises."""

        self._emit("start")
        context = _WorkflowContext(self)
        try:
            value = self.script(context)
        except WorkflowError as error:
            if error.code == "CANCELLED":
                self._emit("end", stop_reason=STOP_CANCELLED)
                return WorkflowResult(None, STOP_CANCELLED, str(error),
                                      self.agents_started)
            self._emit("end", stop_reason=STOP_ERROR)
            return WorkflowResult(None, STOP_ERROR, str(error), self.agents_started)
        except Exception as error:  # a plain script error is still a settled run
            self._emit("end", stop_reason=STOP_ERROR)
            return WorkflowResult(None, STOP_ERROR, str(error), self.agents_started)
        self._emit("end", stop_reason=STOP_COMPLETED)
        return WorkflowResult(value, STOP_COMPLETED, None, self.agents_started)

    def dispose(self) -> None:
        self.disposed = True


class WorkflowEvents:
    """Observe-only events. Listeners receive a snapshot, never the live run."""

    def __init__(self):
        self.listeners = []
        self.log = []

    def on(self, listener: Callable) -> None:
        self.listeners.append(listener)

    def emit(self, kind: str, run: WorkflowRun, **extra) -> None:
        payload = {"kind": kind, "run": {"id": run.id, "meta": run.meta}}
        payload.update(extra)
        self.log.append(kind)
        for listener in self.listeners:
            listener(dict(payload))  # each listener gets its own shallow clone


def start_workflow(service: SubagentService, meta: WorkflowMeta, script: Callable,
                   parent: AgentHandle, args=None, max_total_agents: int = 8,
                   events: WorkflowEvents | None = None) -> WorkflowRun:
    if not isinstance(meta.name, str) or not meta.name:
        raise WorkflowError("INVALID_META", "meta.name is required", fatal=True)
    if not isinstance(meta.description, str) or not meta.description:
        raise WorkflowError("INVALID_META", "meta.description is required", fatal=True)
    return WorkflowRun(service, meta, script, parent, args=args,
                       max_total_agents=max_total_agents, events=events)


# ---------------------------------------------------------------------------
# Teaching fixtures and demo
# ---------------------------------------------------------------------------

def echo_handler(prompt):
    return f"echo: {prompt}"


def counting_handler(prompt):
    if "fail" in prompt:
        raise ChildFailure("provider could not finish this run")
    return f"done: {prompt}"


def structured_handler(prompt):
    return {"summary": f"done: {prompt}"}


def continuable_factory(service, request):
    def handler(transcript):
        last = transcript[-1]["text"] if transcript else ""
        return f"child reply to {last!r}"

    return ContinuableChild(service, request, handler)


def main() -> None:
    service = SubagentService(max_depth=1, max_active_subagents=2)
    service.register(Provider("spawn", counting_handler))
    service.register(Provider(
        "deep",
        structured_handler,
        capabilities=frozenset({"agentOptions", "outputSchema", "depthLimit"}),
    ))
    service.register(Provider("resumable", echo_handler, continuable=continuable_factory))

    root = AgentHandle("session-root", depth=0, name="lead")

    print("== capability flags are checked before the run starts ==")
    bad = StartRequest(provider="spawn", prompt="hi", parent=root,
                       output_schema={"type": "object"})
    try:
        service.start(bad)
    except SubagentError as error:
        print("spawn + outputSchema ->", error.code)
    good = StartRequest(provider="deep", prompt="hi", parent=root,
                        output_schema={"type": "object"})
    print("deep + outputSchema ->", service.start(good).ok)

    print("\n== depth 1 allows a direct child but not a grandchild ==")
    service.start(StartRequest(provider="spawn", prompt="direct", parent=root))
    grandchild = AgentHandle("session-c", depth=1, name="child")
    try:
        service.start(StartRequest(provider="spawn", prompt="deeper", parent=grandchild))
    except SubagentError as error:
        print("grandchild ->", error.code)
    allowed = StartRequest(provider="deep", prompt="deeper", parent=grandchild, max_depth=2)
    print("with maxDepth=2 ->", service.start(allowed).ok)

    print("\n== one-shot failure is a stop reason, not an exception ==")
    failed = service.start(StartRequest(provider="spawn", prompt="please fail", parent=root))
    print("stop_reason:", failed.stop_reason, "| ok:", failed.ok)

    print("\n== a continuable child keeps a queue-minus-delivered mailbox ==")
    child = service.spawn_continuable(
        StartRequest(provider="resumable", prompt="start", parent=root)
    )
    print("after spawn, pending:", [m["text"] for m in child.pending()])
    print("active slots:", service.active_slots())
    print("turn 1:", child.run_turn())
    child.send("second question")
    print("pending after send:", [m["text"] for m in child.pending()])
    print("turn 2:", child.run_turn())
    print("transcript roles:", [row["role"] for row in child.transcript])
    child.settle()
    print("after settle, active slots:", service.active_slots())

    print("\n== capacity is refused, not queued ==")
    first = service.spawn_continuable(StartRequest(provider="resumable", prompt="a", parent=root))
    second = service.spawn_continuable(StartRequest(provider="resumable", prompt="b", parent=root))
    try:
        service.spawn_continuable(StartRequest(provider="resumable", prompt="c", parent=root))
    except SubagentError as error:
        print("third child ->", error.code)
    first.settle()
    second.settle()

    print("\n== the task board is a revision-checked DAG ==")
    board = TaskBoard()
    design = board.create("design", write_scopes=("src/",))
    build = board.create("build", blocked_by=(design.id,), write_scopes=("src/",))
    print("build ready?", board.ready(build.id))
    updated = board.update(design.id, design.revision, status=TASK_COMPLETED)
    print("design revision:", updated.revision, "| build ready?", board.ready(build.id))
    try:
        board.update(design.id, design.revision, status=TASK_PENDING)
    except SubagentError as error:
        print("stale revision ->", error.code)
    try:
        board.create("cycle", blocked_by=("task-1", "task-99"))
    except SubagentError as error:
        print("unknown blocker ->", error.code)

    print("\n== workflow runs report a closed stopReason ==")
    events = WorkflowEvents()
    seen = []
    events.on(lambda payload: seen.append(payload["kind"]))
    meta = WorkflowMeta(name="demo", description="two agents in parallel")
    run = start_workflow(
        service, meta,
        lambda ctx: ctx.parallel(
            lambda: ctx.agent("first"), lambda: ctx.agent("second"),
        ),
        root, max_total_agents=2, events=events,
    )
    result = run.execute()
    run.dispose()
    print("stop_reason:", result.stop_reason, "| value:", result.value,
          "| agentsStarted:", result.agents_started)
    print("observed events:", seen)

    print("\n== the agent ceiling is fatal, a failed child is not ==")
    run = start_workflow(
        service, meta,
        lambda ctx: [ctx.agent("a"), ctx.agent("b"), ctx.agent("c")],
        root, max_total_agents=2,
    )
    result = run.execute()
    print("ceiling ->", result.stop_reason, "|", result.error)
    run = start_workflow(
        service, meta,
        lambda ctx: ctx.parallel(
            lambda: ctx.agent("ok"), lambda: ctx.agent("please fail"),
        ),
        root,
    )
    result = run.execute()
    print("child failure ->", result.stop_reason, "| value:", result.value)
    run.dispose()

    print("\n== cancellation settles as cancelled, never as a value ==")
    run = start_workflow(service, meta, lambda ctx: ctx.agent("slow"), root)
    run.cancel("user pressed stop")
    result = run.execute()
    print("stop_reason:", result.stop_reason, "| value:", result.value)

    print("\n== listeners observe snapshots, not the live run ==")
    captured = []
    events.on(lambda payload: captured.append(payload))
    fresh = start_workflow(service, meta, lambda ctx: "ok", root, events=events)
    fresh.execute()
    print("payload keys:", sorted(captured[-1]))
    print("has cancel?", hasattr(captured[-1]["run"], "cancel"))


if __name__ == "__main__":
    main()