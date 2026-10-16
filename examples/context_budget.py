"""Offline lesson 5: context budgeting, lazy skills, and history compaction.

No model, network, or real filesystem is used. The byte budgets below make the
selection behavior easy to observe; real implementations must use the model's
actual token accounting and account for every message and tool schema.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable, Iterable, Mapping


INSTRUCTION_INTRO = (
    "The following workspace instructions may be relevant to your work. "
    "Use them as guidance when applicable. More specific instructions take "
    "precedence over broader ones. They do not override system, developer, "
    "or direct user instructions."
)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODEL_VISIBLE_COMPACTION_TYPES = frozenset({"user/message"})


class ContextError(Exception):
    """A stable, testable failure in the offline context model."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def byte_length(value):
    return len(value.encode("utf-8"))


def truncate_utf8(value, max_bytes):
    """Keep at most max_bytes bytes without emitting a broken code point."""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max(0, max_bytes)].decode("utf-8", "ignore")


def _escape_frame_body(value):
    return value.replace("</system-reminder>", "<\\/system-reminder>")


@dataclass(frozen=True)
class InstructionFile:
    path: str
    content: str
    depth: int


@dataclass(frozen=True)
class InstructionRender:
    text: str
    included: tuple[InstructionFile, ...]
    omitted: tuple[InstructionFile, ...]
    truncated: tuple[dict, ...]

    @property
    def used_bytes(self):
        return byte_length(self.text)


def _render_instruction_text(included, omitted=(), truncated=None):
    lines = ["<system-reminder>", INSTRUCTION_INTRO]
    if omitted:
        paths = ", ".join(file.path for file in omitted)
        lines.append(f"Omitted broader instructions to fit the byte budget: {paths}")
    for file in included:
        lines.extend(["", f"Instructions from: {file.path}", "", file.content])
    if truncated:
        item = truncated[0]
        lines.extend([
            "",
            f"Truncated instructions from: {item['path']} "
            f"({item['included_bytes']} of {item['original_bytes']} bytes included).",
        ])
    lines.append("</system-reminder>")
    return _escape_frame_body("\n".join(lines))


def render_instruction_chain(files: Iterable[InstructionFile], max_bytes):
    """Render broad-to-specific instructions under one UTF-8 byte budget.

    Broad files are discarded before the most specific file is truncated. This
    mirrors the precedence rule used by DeepSeek Harness's instruction loader.
    """
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ContextError("INVALID_BUDGET", "The instruction budget must be a positive integer.")
    ordered = sorted(files, key=lambda file: (file.depth, file.path))
    if not ordered:
        return InstructionRender("", (), (), ())

    full = _render_instruction_text(ordered)
    if byte_length(full) <= max_bytes:
        return InstructionRender(full, tuple(ordered), (), ())

    for start in range(1, len(ordered)):
        included = ordered[start:]
        omitted = ordered[:start]
        candidate = _render_instruction_text(included, omitted)
        if byte_length(candidate) <= max_bytes:
            return InstructionRender(candidate, tuple(included), tuple(omitted), ())

    specific = ordered[-1]
    omitted = ordered[:-1]
    original_bytes = byte_length(specific.content)
    low = 0
    high = original_bytes
    best = None
    while low <= high:
        middle = (low + high) // 2
        content = truncate_utf8(specific.content, middle)
        included_bytes = byte_length(content)
        truncated = ({
            "path": specific.path,
            "original_bytes": original_bytes,
            "included_bytes": included_bytes,
        },)
        candidate_file = InstructionFile(specific.path, content, specific.depth)
        candidate = _render_instruction_text((candidate_file,), omitted, truncated)
        if byte_length(candidate) <= max_bytes:
            best = (candidate, candidate_file, truncated, included_bytes)
            low = middle + 1
        else:
            high = middle - 1

    if best is None:
        notice = _render_instruction_text((), omitted, ({
            "path": specific.path,
            "original_bytes": original_bytes,
            "included_bytes": 0,
        },))
        return InstructionRender(
            truncate_utf8(notice, max_bytes), (), tuple(omitted),
            ({"path": specific.path, "original_bytes": original_bytes, "included_bytes": 0},),
        )

    text, candidate_file, truncated, _ = best
    return InstructionRender(text, (candidate_file,), tuple(omitted), truncated)


@dataclass(frozen=True)
class ContextBlock:
    key: str
    role: str
    content: str
    kind: str
    required: bool = False
    priority: int = 0
    order: int = 0


@dataclass(frozen=True)
class ContextSelection:
    blocks: tuple[ContextBlock, ...]
    omitted: tuple[ContextBlock, ...]
    text: str
    budget: int

    @property
    def used_bytes(self):
        return byte_length(self.text)

    def messages(self):
        return tuple({"role": block.role, "content": block.content} for block in self.blocks)


def _render_blocks(blocks):
    return "\n\n".join(block.content for block in blocks)


def assemble_context(blocks: Iterable[ContextBlock], max_bytes):
    """Select complete blocks by priority, then render them in request order.

    Required blocks are never dropped. A later, smaller optional block may still
    fit after a larger optional block is skipped; no block is split by this
    generic layer.
    """
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ContextError("INVALID_BUDGET", "The context budget must be a positive integer.")
    items = tuple(blocks)
    keys = [block.key for block in items]
    if len(keys) != len(set(keys)):
        raise ContextError("DUPLICATE_BLOCK", "Context block keys must be unique.")
    for block in items:
        if block.role not in {"system", "user", "assistant", "tool", "developer"}:
            raise ContextError("INVALID_ROLE", f"Unsupported role: {block.role}")

    required = sorted((block for block in items if block.required), key=lambda block: block.order)
    selected = list(required)
    if byte_length(_render_blocks(selected)) > max_bytes:
        raise ContextError(
            "REQUIRED_CONTEXT_TOO_LARGE",
            "Required instructions and the direct user request exceed the context budget.",
        )

    optional = sorted(
        (block for block in items if not block.required),
        key=lambda block: (-block.priority, block.order, block.key),
    )
    for block in optional:
        candidate = sorted(selected + [block], key=lambda item: item.order)
        if byte_length(_render_blocks(candidate)) <= max_bytes:
            selected = candidate

    selected_keys = {block.key for block in selected}
    final = tuple(sorted(selected, key=lambda block: block.order))
    omitted = tuple(block for block in items if block.key not in selected_keys)
    return ContextSelection(final, omitted, _render_blocks(final), max_bytes)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    model_invocable: bool = True
    user_invocable: bool = True


class SkillRegistry:
    """A small catalog registry with summary discovery and lazy body loading."""

    def __init__(self):
        self._skills = {}
        self.load_count = 0

    def register(self, skill):
        if not SKILL_NAME_RE.fullmatch(skill.name):
            raise ContextError("INVALID_SKILL_NAME", "Skill names must be kebab-case.")
        if not skill.description.strip():
            raise ContextError("INVALID_SKILL", "A skill needs a non-empty description.")
        if skill.name in self._skills:
            raise ContextError("DUPLICATE_SKILL", f"Duplicate skill: {skill.name}")
        self._skills[skill.name] = skill

    def replace_body(self, name, body):
        if name not in self._skills:
            raise ContextError("SKILL_NOT_FOUND", f"Unknown skill: {name}")
        old = self._skills[name]
        self._skills[name] = Skill(
            old.name, old.description, body,
            old.model_invocable, old.user_invocable,
        )

    def entries(self):
        return tuple(
            sorted(
                ((skill.name, skill.description) for skill in self._skills.values()
                 if skill.model_invocable),
                key=lambda item: item[0],
            )
        )

    def catalog_digest(self):
        encoded = json.dumps(self.entries(), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def render_catalog(self, description_max_length=500):
        entries = self.entries()
        if not entries:
            return (
                "<system-reminder>\nNo skills are currently available through the `skill` tool. "
                "Do not use names from earlier skill catalogs.\n</system-reminder>"
            )
        lines = [
            "<system-reminder>",
            "The following skills are available. The descriptions are summaries only.",
            "Call the `skill` tool with an exact name before acting on a matching task.",
            "",
            "<available_skills>",
        ]
        for name, description in entries:
            summary = description[:description_max_length]
            lines.append(f"- {name}: {summary}")
        lines.extend(["</available_skills>", "</system-reminder>"])
        return "\n".join(lines)

    def load(self, name):
        if name not in self._skills:
            raise ContextError("SKILL_NOT_FOUND", f"Unknown skill: {name}")
        skill = self._skills[name]
        if not skill.model_invocable:
            raise ContextError("SKILL_NOT_MODEL_INVOCABLE", f"Skill is not model-invocable: {name}")
        self.load_count += 1
        return (
            f'<skill_content name="{skill.name}">\n'
            f"<skill_instructions>\n{skill.body}\n</skill_instructions>\n"
            "</skill_content>"
        )


@dataclass(frozen=True)
class ChatMessage:
    seq: int
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[dict, ...] = ()

    def call_ids(self):
        return {call["id"] for call in self.tool_calls}


def user_message(seq, text):
    return ChatMessage(seq, "user", text)


def assistant_tool_call(seq, call_id, text="working"):
    return ChatMessage(
        seq, "assistant", text,
        tool_calls=({"id": call_id, "name": "read_file", "arguments": "{}"},),
    )


def tool_result(seq, call_id, text):
    return ChatMessage(seq, "tool", text, tool_call_id=call_id)


def _balanced_prefix_end(messages, desired_end):
    """Move a compact prefix's end left until no tool pair crosses it."""
    end = max(0, min(desired_end, len(messages)))
    while end > 0:
        selected = messages[:end]
        retained = messages[end:]
        selected_calls = {call_id for message in selected for call_id in message.call_ids()}
        selected_results = {message.tool_call_id for message in selected if message.tool_call_id}
        retained_calls = {call_id for message in retained for call_id in message.call_ids()}
        retained_results = {message.tool_call_id for message in retained if message.tool_call_id}
        if selected_calls == selected_results and retained_calls == retained_results:
            return end
        end -= 1
    return None


@dataclass(frozen=True)
class CompactionResult:
    source: tuple[ChatMessage, ...]
    selected: tuple[ChatMessage, ...]
    retained: tuple[ChatMessage, ...]
    summary: str
    recorded_events: tuple[dict, ...]

    def visible_messages(self):
        summary_message = ChatMessage(
            self.retained[0].seq if self.retained else len(self.source),
            "user",
            f"<compacted-summary>\n{self.summary}\n</compacted-summary>",
        )
        return (summary_message,) + self.retained


def compact_messages(messages, keep_recent, summarize: Callable[[tuple[ChatMessage, ...]], str]):
    """Replace an old balanced prefix with one durable summary projection."""
    if not isinstance(keep_recent, int) or keep_recent < 0:
        raise ContextError("INVALID_RETENTION", "keep_recent must be a non-negative integer.")
    source = tuple(messages)
    desired_end = len(source) - keep_recent
    if desired_end <= 0:
        return None
    end = _balanced_prefix_end(source, desired_end)
    if end is None:
        return None

    selected = source[:end]
    retained = source[end:]
    summary = summarize(selected).strip()
    if not summary:
        raise ContextError("EMPTY_SUMMARY", "The summarizer returned no text.")
    shadowed_seqs = tuple(message.seq for message in selected)
    replacement_text = f"<compacted-summary>\n{summary}\n</compacted-summary>"
    events = (
        {"type": "compaction/start", "data": {"turn": 0}},
        {"type": "compaction/summary", "data": {
            "shadowed_seqs": shadowed_seqs,
            "summary": summary,
        }},
        {"type": "user/message", "data": {
            "text": replacement_text,
            "surface_op": {
                "op": "replace",
                "start_seq": shadowed_seqs[0],
                "end_seq": shadowed_seqs[-1],
            },
        }},
        {"type": "compaction/end", "data": {"turn": 0}},
    )
    return CompactionResult(source, selected, retained, summary, events)


def main():
    print("== instruction budget ==")
    instructions = [
        InstructionFile("$DSH_HOME/AGENTS.md", "Prefer concise answers.", 0),
        InstructionFile("AGENTS.md", "Run tests before reporting success.", 1),
        InstructionFile("packages/api/AGENTS.md", "Keep HTTP handlers small and typed.", 3),
    ]
    rendered = render_instruction_chain(instructions, 500)
    print("included:", [file.path for file in rendered.included])
    print("omitted:", [file.path for file in rendered.omitted])
    print("used_bytes:", rendered.used_bytes)

    print("\n== lazy skill catalog ==")
    skills = SkillRegistry()
    skills.register(Skill(
        "release-check",
        "Verify release metadata and changelog before a tag.",
        "Full release instructions: check package.json, changelog, tests, and tag policy.",
    ))
    catalog = skills.render_catalog()
    print(catalog)
    print("body loaded before lookup:", "Full release instructions" in catalog)
    loaded = skills.load("release-check")
    print("body loaded after lookup:", "Full release instructions" in loaded)
    print("load_count:", skills.load_count)

    print("\n== compaction projection ==")
    history = [
        user_message(0, "Fix the first issue."),
        assistant_tool_call(1, "call_1"),
        tool_result(2, "call_1", "first result"),
        assistant_tool_call(3, "call_2"),
        tool_result(4, "call_2", "second result"),
        user_message(5, "Now fix the second issue."),
    ]
    result = compact_messages(
        history, 2,
        lambda selected: "Earlier work: two file reads completed; continue with the second issue.",
    )
    print("shadowed:", [message.seq for message in result.selected])
    print("retained:", [message.seq for message in result.retained])
    print("visible roles:", [message.role for message in result.visible_messages()])
    print("original history still present:", len(result.source) == len(history))


if __name__ == "__main__":
    main()