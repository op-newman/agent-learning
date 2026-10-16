"""Offline lesson 6: streaming settlement, retry, cancellation, and tool scheduling.

The module models the protocol rather than calling a provider. It keeps the
same important invariants as the real harness: a stream ends in one terminal
finish chunk, failed attempts never become surface messages, retries are made
durable before waiting, cancellation is cooperative, and parallel-safe tool
calls are bounded while exclusive calls act as barriers.
"""

from dataclasses import dataclass, field
import math
from typing import Callable, Iterable, Mapping, Sequence


class LoopError(Exception):
    """A stable failure in the offline loop model."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Cancelled(LoopError):
    def __init__(self, message="The operation was cancelled."):
        super().__init__("CANCELLED", message)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int | None = None


@dataclass(frozen=True)
class FinishReason:
    kind: str
    code: str | None = None
    message: str = ""


@dataclass(frozen=True)
class BlockStart:
    index: int
    block_type: str
    type: str = field(default="block-start", init=False)


@dataclass(frozen=True)
class TextDelta:
    index: int
    text: str
    type: str = field(default="text-delta", init=False)


@dataclass(frozen=True)
class ReasoningDelta:
    index: int
    text: str
    type: str = field(default="reasoning-delta", init=False)


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    call_id: str
    name: str
    arguments_delta: str
    type: str = field(default="tool-call-delta", init=False)


@dataclass(frozen=True)
class BlockEnd:
    index: int
    block: Mapping
    type: str = field(default="block-end", init=False)


@dataclass(frozen=True)
class UsageChunk:
    usage: TokenUsage
    type: str = field(default="usage", init=False)


@dataclass(frozen=True)
class FinishChunk:
    reason: FinishReason
    replay_state: Mapping | None = None
    type: str = field(default="finish", init=False)


@dataclass(frozen=True)
class StreamSettlement:
    """The durable result of one provider attempt."""

    chunks: tuple
    blocks: tuple
    finish: FinishReason
    usage: TokenUsage | None
    replay_state: Mapping | None

    @property
    def succeeded(self):
        return self.finish.kind in {"stop", "tool-calls", "max-tokens"}

    @property
    def event_type(self):
        return "assistant/message" if self.succeeded else "assistant/attempt"


def _partial_from_block(block):
    if not isinstance(block, Mapping):
        raise LoopError("INVALID_BLOCK", "A block must be a mapping.")
    block_type = block.get("type")
    if block_type == "text":
        return {"block_type": "text", "text": str(block.get("text", ""))}
    if block_type == "reasoning":
        return {"block_type": "reasoning", "text": str(block.get("text", ""))}
    if block_type == "tool-call":
        return {
            "block_type": "tool-call",
            "call_id": str(block.get("id", "")),
            "name": str(block.get("name", "")),
            "arguments": str(block.get("arguments", "")),
        }
    raise LoopError("UNKNOWN_BLOCK", f"Unsupported block type: {block_type!r}")


def assemble_chunks(chunks: Iterable):
    """Fold raw chunks into blocks, usage, and a terminal finish reason.

    `max-tokens` drops every tool call: a truncated tool call may contain
    invalid JSON or only part of its arguments, so executing it would be unsafe.
    """

    partials = {}
    order = []
    usage = None
    finish = None
    replay_state = None

    def ensure(index, block_type):
        partial = partials.get(index)
        if partial is None:
            partial = {"block_type": block_type, "text": "", "closed": False}
            partials[index] = partial
            order.append(index)
        return partial

    for chunk in chunks:
        if finish is not None:
            raise LoopError("CHUNKS_AFTER_FINISH", "No chunk may follow terminal finish.")
        if isinstance(chunk, BlockStart):
            partial = ensure(chunk.index, chunk.block_type)
            if partial.get("text") or partial.get("closed"):
                raise LoopError("BLOCK_INDEX_REUSED", "A live block index cannot be restarted.")
        elif isinstance(chunk, TextDelta):
            partial = ensure(chunk.index, "text")
            if partial["block_type"] != "text":
                raise LoopError("BLOCK_TYPE_MISMATCH", "Text delta targets a non-text block.")
            partial["text"] += chunk.text
        elif isinstance(chunk, ReasoningDelta):
            partial = ensure(chunk.index, "reasoning")
            if partial["block_type"] != "reasoning":
                raise LoopError("BLOCK_TYPE_MISMATCH", "Reasoning delta targets a non-reasoning block.")
            partial["text"] += chunk.text
        elif isinstance(chunk, ToolCallDelta):
            partial = ensure(chunk.index, "tool-call")
            if partial["block_type"] != "tool-call":
                raise LoopError("BLOCK_TYPE_MISMATCH", "Tool delta targets a non-tool block.")
            partial.setdefault("call_id", chunk.call_id)
            partial.setdefault("name", chunk.name)
            partial.setdefault("arguments", "")
            partial["arguments"] += chunk.arguments_delta
        elif isinstance(chunk, BlockEnd):
            partial = ensure(chunk.index, chunk.block.get("type", "text"))
            if partial["closed"]:
                continue
            partial.update(_partial_from_block(chunk.block))
            partial["closed"] = True
        elif isinstance(chunk, UsageChunk):
            usage = chunk.usage
        elif isinstance(chunk, FinishChunk):
            finish = chunk.reason
            replay_state = chunk.replay_state
        else:
            raise LoopError("UNKNOWN_CHUNK", f"Unknown chunk: {chunk!r}")

    if finish is None:
        raise LoopError("MISSING_TERMINAL_FINISH", "A stream must end with a finish chunk.")

    blocks = []
    for index in order:
        partial = partials[index]
        block_type = partial["block_type"]
        if block_type == "text":
            blocks.append({"type": "text", "text": partial["text"]})
        elif block_type == "reasoning":
            blocks.append({"type": "reasoning", "text": partial["text"]})
        elif block_type == "tool-call":
            block = {
                "type": "tool-call",
                "id": partial.get("call_id", f"call-{index}"),
                "name": partial.get("name", ""),
                "arguments": partial.get("arguments", ""),
            }
            if finish.kind != "max-tokens":
                blocks.append(block)
        else:
            raise LoopError("UNKNOWN_BLOCK", f"Unsupported block type: {block_type!r}")

    return StreamSettlement(tuple(chunks), tuple(blocks), finish, usage, replay_state)


def interrupted_blocks(chunks: Iterable):
    """Keep only visibly streamed text/reasoning before a hard interruption.

    Tool calls are deliberately omitted: an interrupted call was never safely
    dispatched, and retaining it as a surface message would require inventing a
    tool result.
    """

    try:
        settlement = assemble_chunks(chunks)
    except LoopError as error:
        if error.code != "MISSING_TERMINAL_FINISH":
            raise
    kept = []
    for chunk in chunks:
        if isinstance(chunk, TextDelta):
            for block in kept:
                if block["type"] == "text" and block["index"] == chunk.index:
                    block["text"] += chunk.text
                    break
            else:
                kept.append({"type": "text", "index": chunk.index, "text": chunk.text})
        elif isinstance(chunk, ReasoningDelta):
            for block in kept:
                if block["type"] == "reasoning" and block["index"] == chunk.index:
                    block["text"] += chunk.text
                    break
            else:
                kept.append({"type": "reasoning", "index": chunk.index, "text": chunk.text})
    return tuple(
        {"type": block["type"], "text": block["text"]}
        for block in kept
        if block["text"].strip()
    )


class CancellationToken:
    def __init__(self, cancelled=False):
        self.cancelled = bool(cancelled)

    def cancel(self):
        self.cancelled = True

    def raise_if_cancelled(self):
        if self.cancelled:
            raise Cancelled()


@dataclass(frozen=True)
class RetryPolicy:
    mode: str = "normal"
    max_retries: int = 5
    retryable_codes: tuple[str, ...] = (
        "EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT",
    )
    initial_delay_ms: int = 500
    max_delay_ms: int = 10_000
    jitter_ratio: float = 0.1

    def may_retry(self, attempt, finish):
        if self.mode == "always":
            return True
        return attempt <= self.max_retries and finish.code in self.retryable_codes


def retry_delay_ms(policy, attempt, random_unit=0.5):
    """Return capped exponential backoff with symmetric jitter.

    `attempt` starts at 1 and `random_unit` is in [0, 1]. The default 0.5
    removes jitter, which keeps examples and tests deterministic.
    """

    if not 0 <= random_unit <= 1:
        raise ValueError("random_unit must be between 0 and 1")
    base = min(policy.max_delay_ms, policy.initial_delay_ms * (2 ** max(0, attempt - 1)))
    factor = 1 + (random_unit * 2 - 1) * policy.jitter_ratio
    return max(1, int(base * factor))


@dataclass(frozen=True)
class AttemptRunResult:
    settlement: StreamSettlement | None
    events: tuple[dict, ...]
    attempts: int
    cancelled: bool = False
    delivered_text: str = ""

    @property
    def succeeded(self):
        return bool(self.settlement and self.settlement.succeeded and not self.cancelled)


def run_attempt_with_retry(
    stream_factory: Callable[[int], Sequence],
    policy=RetryPolicy(),
    cancel_token=None,
    sleep: Callable[[int, CancellationToken], None] | None = None,
    random_unit: Callable[[], float] = lambda: 0.5,
):
    """Run one provider attempt, then retry the same open step if policy allows.

    A planned retry is recorded before waiting, mirroring the durable-order
    rule. Cancellation interrupts both a retry wait and a stream that observes
    the same token.
    """

    token = cancel_token or CancellationToken()
    wait = sleep or (lambda delay, signal: None)
    events = []
    delivered = ""
    attempt = 1
    while True:
        if token.cancelled:
            return AttemptRunResult(None, tuple(events), attempt - 1, True, delivered)
        chunks = []
        try:
            for chunk in stream_factory(attempt):
                token.raise_if_cancelled()
                chunks.append(chunk)
            settlement = assemble_chunks(chunks)
        except Cancelled:
            delivered = "".join(
                block["text"] for block in interrupted_blocks(chunks)
                if block["type"] == "text"
            )
            return AttemptRunResult(None, tuple(events), attempt - 1, True, delivered)
        except Exception as error:
            failure = FinishReason("error", getattr(error, "code", "STREAM_ERROR"), str(error))
            settlement = assemble_chunks(chunks + [FinishChunk(failure)])
            events.append({"type": "assistant/attempt", "data": {"finish": failure.kind}})

        if settlement.succeeded:
            events.append({
                "type": "assistant/message",
                "data": {
                    "blocks": settlement.blocks,
                    "finish": settlement.finish.kind,
                    "empty": not settlement.blocks,
                },
            })
            return AttemptRunResult(settlement, tuple(events), attempt, False)

        events.append({"type": "assistant/attempt", "data": {"finish": settlement.finish.kind}})
        if not policy.may_retry(attempt, settlement.finish):
            return AttemptRunResult(settlement, tuple(events), attempt, False)

        delay = retry_delay_ms(policy, attempt, random_unit())
        events.append({
            "type": "llm/retry",
            "data": {"attempt": attempt, "code": settlement.finish.code, "delay_ms": delay},
        })
        try:
            wait(delay, token)
            token.raise_if_cancelled()
        except Cancelled:
            return AttemptRunResult(settlement, tuple(events), attempt, True)
        events.append({"type": "llm/retry-started", "data": {"attempt": attempt + 1}})
        attempt += 1


@dataclass(frozen=True)
class ToolCallSpec:
    call_id: str
    name: str
    execution_mode: str
    duration: float


@dataclass(frozen=True)
class ScheduledToolCall:
    call_id: str
    start: float
    end: float


def schedule_tool_calls(calls: Sequence[ToolCallSpec], max_parallel=10):
    """Schedule tool calls with bounded overlap and exclusive barriers.

    Parallel-safe calls use the earliest free lane. An exclusive call waits for
    every earlier lane, runs alone, and moves every lane to its end, so both its
    predecessors and its successors respect the barrier.
    """

    if not isinstance(max_parallel, int) or max_parallel <= 0:
        raise LoopError("INVALID_PARALLELISM", "max_parallel must be a positive integer.")
    lanes = [0.0] * max_parallel
    scheduled = []
    for call in calls:
        if call.duration < 0:
            raise LoopError("INVALID_DURATION", "Tool duration must be non-negative.")
        if call.execution_mode == "exclusive":
            start = max(lanes)
            end = start + call.duration
            lanes = [end] * max_parallel
        elif call.execution_mode == "parallel-safe":
            lane = min(range(max_parallel), key=lambda index: (lanes[index], index))
            start = lanes[lane]
            end = start + call.duration
            lanes[lane] = end
        else:
            raise LoopError(
                "INVALID_EXECUTION_MODE",
                "execution_mode must be 'parallel-safe' or 'exclusive'.",
            )
        scheduled.append(ScheduledToolCall(call.call_id, start, end))
    return tuple(scheduled)


def max_observed_concurrency(scheduled):
    """Return the maximum number of scheduled intervals overlapping at a time."""

    events = []
    for call in scheduled:
        events.append((call.start, 1))
        events.append((call.end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    return peak


def reconcile_unfinished_tools(
    calls: Sequence[ToolCallSpec],
    started_ids: Iterable[str],
    results: Mapping[str, Mapping],
    aborted_ids: Iterable[str] = (),
):
    """Create explicit error results for calls that have no durable result."""

    started = set(started_ids)
    aborted = set(aborted_ids)
    reconciled = dict(results)
    for call in calls:
        if call.call_id in reconciled:
            continue
        if call.call_id in aborted:
            reconciled[call.call_id] = {
                "is_error": True,
                "code": "ABORTED_BEFORE_DISPATCH",
                "content": "Error: tool call aborted before dispatch",
            }
        elif call.call_id in started:
            reconciled[call.call_id] = {
                "is_error": True,
                "code": "UNKNOWN_OUTCOME",
                "content": (
                    "The tool started, but its outcome is unknown. Re-run only if the "
                    "operation is read-only or idempotent; otherwise verify external state."
                ),
            }
        else:
            reconciled[call.call_id] = {
                "is_error": True,
                "code": "INTERRUPTED_BEFORE_START",
                "content": (
                    "The tool call was interrupted before execution was recorded. "
                    "Retry it if it is still needed."
                ),
            }
    return reconciled


def main():
    print("== successful stream settlement ==")
    stream = [
        BlockStart(0, "text"), TextDelta(0, "hello "), TextDelta(0, "world"),
        BlockEnd(0, {"type": "text", "text": "hello world"}),
        UsageChunk(TokenUsage(10, 2, 12)),
        FinishChunk(FinishReason("stop")),
    ]
    settlement = assemble_chunks(stream)
    print("event:", settlement.event_type)
    print("blocks:", settlement.blocks)
    print("usage:", settlement.usage)

    print("\n== truncated tool call is dropped ==")
    truncated = [
        BlockStart(0, "text"), TextDelta(0, "I will edit the file."),
        BlockStart(1, "tool-call"),
        ToolCallDelta(1, "call-1", "edit", '{"path":"a.py","content":"broken'),
        FinishChunk(FinishReason("max-tokens")),
    ]
    print(assemble_chunks(truncated).blocks)

    print("\n== retry plan is durable before waiting ==")
    attempts = [
        [FinishChunk(FinishReason("error", "SERVER", "temporary"))],
        [TextDelta(0, "recovered"), FinishChunk(FinishReason("stop"))],
    ]
    result = run_attempt_with_retry(lambda number: attempts[number - 1])
    print("attempts:", result.attempts)
    print("events:", [event["type"] for event in result.events])
    print("succeeded:", result.succeeded)

    print("\n== bounded parallel execution and exclusive barrier ==")
    calls = [
        ToolCallSpec("read-a", "read", "parallel-safe", 2),
        ToolCallSpec("read-b", "read", "parallel-safe", 2),
        ToolCallSpec("write", "write", "exclusive", 1),
        ToolCallSpec("test", "bash", "parallel-safe", 3),
    ]
    timeline = schedule_tool_calls(calls, 2)
    for item in timeline:
        print(f"{item.call_id}: {item.start:.1f}-{item.end:.1f}")
    print("peak concurrency:", max_observed_concurrency(timeline))

    print("\n== unfinished tool reconciliation ==")
    reconciled = reconcile_unfinished_tools(
        calls[:3], started_ids={"read-a", "write"}, results={}, aborted_ids={"read-b"},
    )
    for call in calls[:3]:
        print(call.call_id, reconciled[call.call_id]["code"])


if __name__ == "__main__":
    main()