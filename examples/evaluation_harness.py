"""Offline lesson 10: evaluating a model-driven path without a model.

A real evaluation has to answer three different questions, and this module
keeps them apart:

* **What did the agent ask for?**  A scripted model replays a recorded
  transcript, so the tool-call sequence is fixed and inspectable.
* **What actually happened?**  Tools run for real against an in-memory
  workspace, and every call is recorded in a trace.
* **Did it work?**  A check reads the *external* result -- the workspace
  contents and the trace -- instead of trusting the agent's own summary.

The suite then scores the checks and applies a gate. A failing case produces a
reproducible report: which check failed, what was expected, what was observed.

Everything is deterministic, in memory, and offline. It demonstrates an
evaluation protocol, not the real model behaviour that protocol is meant to
measure.
"""

from dataclasses import dataclass, field
from typing import Callable

STOP_COMPLETED = "completed"
STOP_ERROR = "error"


class FixtureExhausted(Exception):
    """The scripted model ran out of recorded turns."""


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class ModelTurn:
    """One recorded model turn: either some tool calls, or a final reply."""

    text: str | None = None
    tool_calls: tuple = ()
    stop_reason: str = STOP_COMPLETED
    error: str | None = None


@dataclass
class Fixture:
    """A recorded transcript. Each session consumes its own copy in FIFO order."""

    name: str
    turns: tuple

    def copy(self) -> "Fixture":
        return Fixture(self.name, tuple(self.turns))


class ScriptedModel:
    """Replays a fixture turn by turn. It never calls a real model."""

    def __init__(self, fixture: Fixture):
        self.fixture = fixture
        self.cursor = 0
        self.requests = []

    @property
    def consumed(self) -> int:
        return self.cursor

    def next_turn(self) -> ModelTurn:
        # The prompt is recorded before the turn is handed out, so a test can
        # assert what the loop actually sent -- not what it meant to send.
        self.requests.append({"turn": self.cursor})
        if self.cursor >= len(self.fixture.turns):
            raise FixtureExhausted(
                f"{self.fixture.name} has {len(self.fixture.turns)} turns"
            )
        turn = self.fixture.turns[self.cursor]
        self.cursor += 1
        return turn


# ---------------------------------------------------------------------------
# Real tools, real workspace
# ---------------------------------------------------------------------------

class Workspace:
    """An in-memory file tree. Tools mutate it; checks read it from outside."""

    def __init__(self, files=None):
        self.files = dict(files or {})

    def snapshot(self) -> dict:
        return dict(self.files)

    def write(self, path: str, content: str) -> None:
        self.files[path] = content

    def read(self, path: str) -> str | None:
        return self.files.get(path)


class ToolRegistry:
    """A real registry: handlers are invoked, and every call is traced."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.handlers = {}
        self.trace = []

    def register(self, name: str, handler: Callable) -> None:
        self.handlers[name] = handler

    def known(self) -> list:
        return sorted(self.handlers)

    def call(self, name: str, args: dict) -> dict:
        if name not in self.handlers:
            result = {"ok": False, "error": f"unknown tool {name!r}"}
        else:
            try:
                payload = self.handlers[name](self.workspace, args)
                result = {"ok": True, "content": payload}
            except Exception as error:  # a tool failure is a result, not a crash
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        self.trace.append({"name": name, "args": dict(args), "result": result})
        return result


# `write_file` is honest: it changes the workspace.
def write_file(workspace: Workspace, args: dict) -> str:
    workspace.write(args["path"], args["content"])
    return f"wrote {args['path']}"


# `claim_write` is the interesting one: it *reports* success without doing
# anything. A check that only read the tool's reply would be fooled by it.
def claim_write(workspace: Workspace, args: dict) -> str:
    return f"wrote {args['path']}"


def read_file(workspace: Workspace, args: dict) -> str:
    content = workspace.read(args["path"])
    if content is None:
        raise FileNotFoundError(args["path"])
    return content


DEFAULT_TOOLS = (
    ("write_file", write_file),
    ("claim_write", claim_write),
    ("read_file", read_file),
)


# ---------------------------------------------------------------------------
# The loop under evaluation
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    reply: str | None = None
    stop_reason: str = STOP_COMPLETED
    error: str | None = None
    tool_trace: tuple = ()

    @property
    def ok(self) -> bool:
        return self.stop_reason == STOP_COMPLETED


class Loop:
    """The real loop: drive the model, execute the tools it asked for."""

    def __init__(self, model: ScriptedModel, registry: ToolRegistry,
                 max_tool_calls: int = 8):
        self.model = model
        self.registry = registry
        self.max_tool_calls = max_tool_calls

    def run(self, prompt: str) -> RunResult:
        del prompt  # the scripted model does not read it; the fixture decides
        while True:
            try:
                turn = self.model.next_turn()
            except FixtureExhausted as error:
                return RunResult(None, STOP_ERROR, f"fixture exhausted: {error}",
                                 tuple(self.registry.trace))
            if turn.stop_reason == STOP_ERROR:
                return RunResult(None, STOP_ERROR, turn.error, tuple(self.registry.trace))
            for call in turn.tool_calls:
                if len(self.registry.trace) >= self.max_tool_calls:
                    return RunResult(None, STOP_ERROR, "tool budget exhausted",
                                     tuple(self.registry.trace))
                self.registry.call(call.name, call.args)
            if not turn.tool_calls:
                return RunResult(turn.text, STOP_COMPLETED, None,
                                 tuple(self.registry.trace))


# ---------------------------------------------------------------------------
# Checks, cases, scoring
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Episode:
    """Everything a check may look at, captured around one run."""

    case_id: str
    fixture_name: str
    run: RunResult
    workspace_before: dict
    workspace_after: dict


def reply_contains(text: str) -> Callable:
    def check(episode: Episode) -> Check:
        reply = episode.run.reply or ""
        return Check(f"reply contains {text!r}", text in reply, f"reply={reply!r}")
    return check


def tool_sequence(names) -> Callable:
    expected = list(names)

    def check(episode: Episode) -> Check:
        actual = [call["name"] for call in episode.run.tool_trace]
        return Check("tool sequence", actual == expected,
                     f"expected={expected} actual={actual}")
    return check


def tool_args(index: int, expected: dict) -> Callable:
    def check(episode: Episode) -> Check:
        actual = episode.run.tool_trace[index]["args"]
        return Check(f"tool[{index}] args", actual == expected,
                     f"expected={expected} actual={actual}")
    return check


def tool_succeeded(index: int) -> Callable:
    def check(episode: Episode) -> Check:
        result = episode.run.tool_trace[index]["result"]
        return Check(f"tool[{index}] succeeded", result["ok"], f"result={result!r}")
    return check


def file_equals(path: str, expected: str) -> Callable:
    def check(episode: Episode) -> Check:
        actual = episode.workspace_after.get(path)
        return Check(f"{path} content", actual == expected,
                     f"expected={expected!r} actual={actual!r}")
    return check


def file_unchanged(path: str) -> Callable:
    def check(episode: Episode) -> Check:
        before = episode.workspace_before.get(path)
        after = episode.workspace_after.get(path)
        return Check(f"{path} unchanged", before == after,
                     f"before={before!r} after={after!r}")
    return check


def stopped_with(reason: str) -> Callable:
    def check(episode: Episode) -> Check:
        return Check(f"stop reason == {reason}", episode.run.stop_reason == reason,
                     f"actual={episode.run.stop_reason!r} error={episode.run.error!r}")
    return check


@dataclass
class Case:
    """One scenario: a fixture, an initial workspace, and its checks."""

    id: str
    fixture: Fixture
    checks: tuple
    files: dict = field(default_factory=dict)
    prompt: str = "do the task"
    tools: tuple = DEFAULT_TOOLS
    max_tool_calls: int = 8

    def run(self) -> Episode:
        workspace = Workspace(self.files)
        registry = ToolRegistry(workspace)
        for name, handler in self.tools:
            registry.register(name, handler)
        model = ScriptedModel(self.fixture.copy())
        before = workspace.snapshot()
        result = Loop(model, registry, self.max_tool_calls).run(self.prompt)
        return Episode(self.id, self.fixture.name, result, before, workspace.snapshot())


@dataclass
class CaseOutcome:
    case_id: str
    fixture_name: str
    checks: tuple
    episode: Episode

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def score(self) -> float:
        if not self.checks:
            return 1.0
        return sum(1 for check in self.checks if check.ok) / len(self.checks)

    def report(self) -> str:
        lines = [f"case {self.case_id} (fixture {self.fixture_name}): "
                 f"{'PASS' if self.passed else 'FAIL'}  score={self.score:.2f}"]
        for check in self.checks:
            mark = "ok  " if check.ok else "FAIL"
            lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if not self.passed:
            lines.append(f"  stop_reason={self.episode.run.stop_reason!r} "
                         f"error={self.episode.run.error!r}")
            lines.append(f"  observed trace: "
                         f"{[call['name'] for call in self.episode.run.tool_trace]}")
        return "\n".join(lines)


@dataclass
class SuiteReport:
    outcomes: tuple

    @property
    def score(self) -> float:
        if not self.outcomes:
            return 1.0
        return sum(outcome.score for outcome in self.outcomes) / len(self.outcomes)

    def gate(self, min_score: float = 1.0) -> bool:
        """A suite passes only when every case passes and the bar is met."""

        return all(outcome.passed for outcome in self.outcomes) and self.score >= min_score

    def report(self) -> str:
        head = f"suite: {len(self.outcomes)} cases, score={self.score:.2f}, " \
               f"gate={'pass' if self.gate() else 'fail'}"
        return "\n".join([head] + [outcome.report() for outcome in self.outcomes])


class Suite:
    """Runs each case in a fresh workspace and collects the outcomes."""

    def __init__(self, cases):
        self.cases = list(cases)

    def run(self) -> SuiteReport:
        outcomes = []
        for case in self.cases:
            episode = case.run()
            outcomes.append(CaseOutcome(
                case_id=case.id,
                fixture_name=case.fixture.name,
                checks=tuple(check(episode) for check in case.checks),
                episode=episode,
            ))
        return SuiteReport(tuple(outcomes))


# ---------------------------------------------------------------------------
# Teaching fixtures and demo
# ---------------------------------------------------------------------------

def honest_fixture() -> Fixture:
    return Fixture("honest", (
        ModelTurn(tool_calls=(
            ToolCall("write_file", {"path": "notes.md", "content": "hello"}),
        )),
        ModelTurn(text="Done. I wrote notes.md."),
    ))


def dishonest_fixture() -> Fixture:
    return Fixture("dishonest", (
        ModelTurn(tool_calls=(
            ToolCall("claim_write", {"path": "notes.md", "content": "hello"}),
        )),
        ModelTurn(text="Done. I wrote notes.md."),
    ))


def read_back_fixture() -> Fixture:
    return Fixture("read-back", (
        ModelTurn(tool_calls=(ToolCall("read_file", {"path": "input.txt"}),)),
        ModelTurn(text="The file says: seed"),
    ))


def runaway_fixture() -> Fixture:
    return Fixture("runaway", tuple(
        ModelTurn(tool_calls=(ToolCall("read_file", {"path": "input.txt"}),))
        for _ in range(5)
    ))


def build_cases() -> list:
    return [
        Case(
            id="writes-the-file",
            fixture=honest_fixture(),
            checks=(tool_sequence(["write_file"]),
                    tool_args(0, {"path": "notes.md", "content": "hello"}),
                    file_equals("notes.md", "hello"),
                    reply_contains("Done")),
        ),
        Case(
            id="truthful-report",
            fixture=dishonest_fixture(),
            # The reply check passes; the workspace check is what catches the lie.
            checks=(reply_contains("Done"), file_equals("notes.md", "hello")),
        ),
        Case(
            id="reads-without-writing",
            fixture=read_back_fixture(),
            files={"input.txt": "seed"},
            checks=(tool_sequence(["read_file"]),
                    tool_succeeded(0),
                    file_unchanged("input.txt"),
                    reply_contains("seed")),
        ),
        Case(
            id="respects-the-tool-budget",
            fixture=runaway_fixture(),
            files={"input.txt": "seed"},
            max_tool_calls=2,
            checks=(stopped_with(STOP_ERROR),
                    tool_sequence(["read_file", "read_file"])),
        ),
    ]


def main() -> None:
    report = Suite(build_cases()).run()
    print(report.report())
    print()
    print("gate(min_score=1.0):", report.gate())
    print("gate(min_score=0.75):", report.gate(0.75))

    print("\n== the same fixture replays identically ==")
    first = Suite([build_cases()[0]]).run().outcomes[0]
    second = Suite([build_cases()[0]]).run().outcomes[0]
    print("same trace:", [c["name"] for c in first.episode.run.tool_trace]
          == [c["name"] for c in second.episode.run.tool_trace])

    print("\n== a lying report is caught by the external check ==")
    outcome = Suite([build_cases()[1]]).run().outcomes[0]
    failed = [check.name for check in outcome.checks if not check.ok]
    print("failed checks:", failed)


if __name__ == "__main__":
    main()