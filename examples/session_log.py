"""Offline lesson 4: append-only session log, message projection, crash repair.

No model, network, or filesystem mutation is used. The durable store below is
deliberately in-memory; a real backend would persist the same JSON lines and
fsync before a checkpoint returns.
"""

import copy
import json
from time import time_ns


SURFACE_TYPES = frozenset({
    "system/message",
    "user/message",
    "assistant/message",
    "tool/result",
})

CRASH_BEFORE_CALL_CHECKPOINT = "before_tool_call_checkpoint"
CRASH_AFTER_CALL_CHECKPOINT = "after_tool_call_checkpoint"
CRASH_AFTER_RESULT_APPEND = "after_tool_result_append"

OUTCOME_UNKNOWN_TEXT = (
    "The tool call was recorded, but no result was durably stored. Its outcome is unknown. "
    "Retry only if the operation is read-only or idempotent; if it may have side effects, "
    "verify external state or ask the user first. Do not retry blindly."
)
NOT_STARTED_TEXT = (
    "The assistant requested this tool call, but no durable call record exists. "
    "The call did not start; retry it if it is still needed."
)


class LogViolation(Exception):
    """A stable failure raised when a log or durable prefix is invalid."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SimulatedCrash(RuntimeError):
    """Stands in for a process that stops at a chosen point in the timeline."""


def plain_json(value):
    """Return a detached JSON value, or reject what a session cannot store."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise LogViolation("NOT_JSON", "Session events must be JSON-serializable.") from None


def default_clock():
    return time_ns() // 1_000_000


class SessionLog:
    """An append-only event log; seq is always the current log length."""

    def __init__(self, events=None, clock=default_clock):
        self._clock = clock
        self._events = []
        if events is not None:
            for expected, event in enumerate(events):
                if event.get("seq") != expected:
                    raise LogViolation("BROKEN_SEQUENCE", "Restored events need contiguous seq numbers.")
                self._events.append(copy.deepcopy(plain_json(event)))

    @property
    def events(self):
        """A detached snapshot; callers cannot mutate the log through it."""
        return copy.deepcopy(self._events)

    @property
    def last_seq(self):
        return len(self._events) - 1

    def append(self, event_type, data, time=None):
        event = {
            "seq": len(self._events),
            "time": self._clock() if time is None else time,
            "type": event_type,
            "data": plain_json(data),
        }
        self._events.append(event)
        return copy.deepcopy(event)

    def derive_messages(self):
        """Project the surface subset of the log into model messages."""
        return [project_event(event) for event in self._events if event["type"] in SURFACE_TYPES]

    def checkpoint(self, durable):
        durable.checkpoint(self._events)


def project_event(event):
    """Map one message-producing event to its model-visible message."""
    event_type = event["type"]
    data = event["data"]
    if event_type == "system/message":
        return {"role": "system", "content": data["text"]}
    if event_type == "user/message":
        return {"role": "user", "content": data["text"]}
    if event_type == "assistant/message":
        message = {"role": "assistant", "content": data["text"]}
        if data.get("tool_calls"):
            message["tool_calls"] = copy.deepcopy(data["tool_calls"])
        return message
    if event_type == "tool/result":
        return {
            "role": "tool",
            "tool_call_id": data["tool_call_id"],
            "content": data["content"],
            "is_error": data["is_error"],
        }
    raise LogViolation("NOT_SURFACE", f"{event_type} does not project to a model message.")


class DurableLog:
    """A tiny persistence boundary; checkpoint models commit plus fsync."""

    def __init__(self):
        self._lines = []

    def checkpoint(self, events):
        lines = [json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) for event in events]
        if lines[:len(self._lines)] != self._lines:
            raise LogViolation("PREFIX_REWRITTEN", "A committed event prefix cannot be rewritten.")
        self._lines = lines

    def replay(self):
        return [json.loads(line) for line in self._lines]

    def line_count(self):
        return len(self._lines)


class ToolExecutor:
    """A host-owned dispatcher; every body invocation is counted."""

    def __init__(self, handlers):
        self._handlers = dict(handlers)
        self.body_invocations = 0

    def run(self, call):
        handler = self._handlers.get(call["name"])
        if handler is None:
            raise LogViolation("UNKNOWN_TOOL", "This tool is not registered.")
        self.body_invocations += 1
        return handler(json.loads(call["arguments"]))


def execute_model_step(log, durable, turn, step, assistant_text, tool_calls, executor, crash_at=None):
    """Record a model answer, checkpoint it, then run the requested tool bodies."""
    log.append("assistant/message", {
        "turn": turn,
        "step": step,
        "text": assistant_text,
        "tool_calls": copy.deepcopy(tool_calls),
    })
    log.checkpoint(durable)
    for call in tool_calls:
        log.append("tool/call", {
            "turn": turn,
            "step": step,
            "call_id": call["id"],
            "name": call["name"],
            "arguments": call["arguments"],
        })
        if crash_at == CRASH_BEFORE_CALL_CHECKPOINT:
            raise SimulatedCrash("process stopped before the dispatch barrier")
        log.checkpoint(durable)
        if crash_at == CRASH_AFTER_CALL_CHECKPOINT:
            raise SimulatedCrash("process stopped after the dispatch barrier, before the body")
        value = executor.run(call)
        log.append("tool/result", {
            "turn": turn,
            "step": step,
            "tool_call_id": call["id"],
            "content": value,
            "is_error": False,
        })
        if crash_at == CRASH_AFTER_RESULT_APPEND:
            raise SimulatedCrash("process stopped after the body returned, before the result was durable")
        log.checkpoint(durable)
    return log


class ToolCallRecovery:
    """Track assistant tool requests that have no durable result yet."""

    def __init__(self):
        self._pending = {}
        self._last = None

    def observe(self, event):
        self._last = event
        event_type = event["type"]
        if event_type in ("turn/start", "turn/end", "step/end"):
            self._pending.clear()
        elif event_type == "assistant/message":
            data = event["data"]
            for call in data.get("tool_calls", []):
                self._pending[call["id"]] = {
                    "turn": data["turn"],
                    "step": data["step"],
                    "call_seq": None,
                }
        elif event_type == "tool/call":
            entry = self._pending.get(event["data"]["call_id"])
            if entry is not None:
                entry["call_seq"] = event["seq"]
        elif event_type == "tool/result":
            data = event["data"]
            entry = self._pending.get(data["tool_call_id"])
            if entry is not None and entry["turn"] == data["turn"] and entry["step"] == data["step"]:
                del self._pending[data["tool_call_id"]]

    def results(self, start_seq, time):
        results = []
        seq = start_seq
        for call_id, entry in self._pending.items():
            started = entry["call_seq"] is not None
            code = "TOOL_OUTCOME_UNKNOWN" if started else "TOOL_NOT_STARTED"
            text = OUTCOME_UNKNOWN_TEXT if started else NOT_STARTED_TEXT
            results.append({
                "seq": seq,
                "time": time,
                "type": "tool/result",
                "data": {
                    "turn": entry["turn"],
                    "step": entry["step"],
                    "tool_call_id": call_id,
                    "content": text,
                    "is_error": True,
                    "error_code": code,
                    "synthetic": True,
                },
            })
            seq += 1
        return results


def interrupted_turn_closers(events):
    """Balance an open tail turn without executing any tool body."""
    recovery = ToolCallRecovery()
    open_turn = None
    open_step = None
    for event in events:
        recovery.observe(event)
        event_type = event["type"]
        if event_type == "turn/start":
            open_turn = event["data"]["turn"]
            open_step = None
        elif event_type == "turn/end":
            open_turn = None
            open_step = None
        elif event_type == "step/start":
            open_step = event["data"]["step"]
        elif event_type == "step/end":
            open_step = None
    if open_turn is None or not events:
        return []
    last = events[-1]
    seq = last["seq"] + 1
    closers = recovery.results(seq, last["time"])
    seq += len(closers)
    if open_step is not None:
        closers.append({
            "seq": seq,
            "time": last["time"],
            "type": "step/end",
            "data": {"turn": open_turn, "step": open_step},
        })
        seq += 1
    closers.append({
        "seq": seq,
        "time": last["time"],
        "type": "turn/end",
        "data": {"turn": open_turn, "reason": {"kind": "interrupted"}},
    })
    return closers


def recover(durable):
    """Replay the durable prefix and append synthetic closer events."""
    events = durable.replay()
    closers = interrupted_turn_closers(events)
    log = SessionLog(events)
    for closer in closers:
        log.append(closer["type"], closer["data"], time=closer["time"])
    if closers:
        log.checkpoint(durable)
    return log, closers


def compact_types(log):
    return [event["type"] for event in log.events]


def crash_scenario(crash_at, handler):
    durable = DurableLog()
    log = SessionLog(clock=lambda: 1_700_000_000_000)
    log.append("turn/start", {"turn": 0})
    log.append("step/start", {"turn": 0, "step": 0})
    log.append("user/message", {"turn": 0, "step": 0, "text": "Write result.txt."})
    executor = ToolExecutor({"write_file": handler})
    try:
        execute_model_step(
            log, durable, 0, 0, "I will write the file.",
            [{"id": "call_1", "name": "write_file", "arguments": '{"path":"result.txt"}'}],
            executor, crash_at=crash_at,
        )
    except SimulatedCrash:
        pass
    restored, closers = recover(durable)
    codes = [closer["data"].get("error_code") for closer in closers if closer["type"] == "tool/result"]
    return restored, executor, codes


def main():
    print("OFFLINE SESSION LOG: no model, no network, no real file mutation.")
    print("\n== normal turn ==")
    durable = DurableLog()
    log = SessionLog(clock=lambda: 1_700_000_000_000)
    log.append("turn/start", {"turn": 0})
    log.append("step/start", {"turn": 0, "step": 0})
    log.append("system/message", {"turn": 0, "step": 0, "text": "You are a careful coding agent."})
    log.append("user/message", {"turn": 0, "step": 0, "text": "Read package.json and tell me the version."})
    executor = ToolExecutor({"read_file": lambda args: '{"name":"learning-demo","version":"0.1.0"}'})
    execute_model_step(
        log, durable, 0, 0, "I will read the file.",
        [{"id": "call_1", "name": "read_file", "arguments": '{"path":"package.json"}'}],
        executor,
    )
    log.append("step/end", {"turn": 0, "step": 0})
    log.append("turn/end", {"turn": 0, "reason": {"kind": "success"}})
    log.checkpoint(durable)
    print("events:", compact_types(log))
    print("derived messages:", json.dumps(log.derive_messages(), ensure_ascii=False, indent=2))

    print("\n== crash before the tool/call checkpoint ==")
    restored, executor, codes = crash_scenario(CRASH_BEFORE_CALL_CHECKPOINT, lambda args: "written")
    print("durable events after repair:", compact_types(restored))
    print("recovery codes:", codes, "| body invocations:", executor.body_invocations)

    print("\n== crash after the tool/call checkpoint ==")
    restored, executor, codes = crash_scenario(CRASH_AFTER_CALL_CHECKPOINT, lambda args: "written")
    print("durable events after repair:", compact_types(restored))
    print("recovery codes:", codes, "| body invocations:", executor.body_invocations)

    print("\n== crash after the body returned, before the result was durable ==")
    restored, executor, codes = crash_scenario(CRASH_AFTER_RESULT_APPEND, lambda args: "written")
    print("durable events after repair:", compact_types(restored))
    print("recovery codes:", codes, "| body invocations:", executor.body_invocations)
    print("last derived message:", json.dumps(restored.derive_messages()[-1], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
