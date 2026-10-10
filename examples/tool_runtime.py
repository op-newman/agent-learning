"""Offline lesson 3: tool registration, validation, policy, and cooperative stop.

The host maps logical file names to trusted fixtures. This is not an OS sandbox.
Only required, non-empty string parameters are supported by this teaching DSL.
"""

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Callable, Mapping


LOGGER = logging.getLogger(__name__)


class ToolFailure(Exception):
    """An expected failure with a stable code and model-facing explanation."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StringParameter:
    name: str
    description: str

    def schema(self):
        return {"type": "string", "description": self.description,
                "minLength": 1, "maxLength": 256}

    def validate(self, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ToolFailure("INVALID_ARGUMENTS", f"{self.name} must be a string of 1 to 256 characters.")


@dataclass
class ToolContext:
    allowed_tools: frozenset[str]
    files: Mapping[str, Path]
    deadline: float | None = None
    cancelled: Event = field(default_factory=Event)
    max_argument_bytes: int = 8192
    max_read_bytes: int = 4096

    def __post_init__(self):
        if self.max_argument_bytes < 1 or self.max_read_bytes < 1:
            raise ValueError("Byte limits must be positive.")

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ToolFailure("CANCELLED", "This tool call was cancelled.")
        if self.deadline is not None and monotonic() >= self.deadline:
            raise ToolFailure("TOOL_TIMEOUT", "The tool call's deadline was reached.")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: tuple[StringParameter, ...]
    handler: Callable[[dict, ToolContext], object]

    def schema(self):
        return {"name": self.name, "description": self.description, "parameters": {
            "type": "object",
            "properties": {p.name: p.schema() for p in self.parameters},
            "required": [p.name for p in self.parameters],
            "additionalProperties": False,
        }}

    def validate(self, arguments):
        names = {p.name for p in self.parameters}
        if not isinstance(arguments, dict) or set(arguments) != names:
            raise ToolFailure("INVALID_ARGUMENTS", "Arguments must contain exactly the declared fields.")
        for parameter in self.parameters:
            parameter.validate(arguments[parameter.name])


class ToolRuntime:
    """Sequential executor. Trace is a local debugging aid, not model content."""

    def __init__(self):
        self._tools = {}
        self.trace = []

    def register(self, definition):
        if definition.name in self._tools:
            raise ValueError(f"Duplicate tool: {definition.name}")
        names = [p.name for p in definition.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate parameter names")
        self._tools[definition.name] = definition

    def catalog(self, context):
        return [tool.schema() for name, tool in self._tools.items()
                if name in context.allowed_tools]

    def execute(self, call, context):
        # A provider adapter must first supply a correlated, structurally valid call.
        if not isinstance(call.id, str) or not call.id or not isinstance(call.name, str) or not call.name:
            raise ValueError("A call needs a non-empty id and name before entering the executor.")
        if not isinstance(call.arguments_json, str):
            raise ValueError("The adapter must preserve arguments as a JSON string.")
        self.trace = ["received"]
        try:
            context.checkpoint()
            self.trace.append("lookup")
            definition = self._tools.get(call.name)
            if definition is None:
                raise ToolFailure("UNKNOWN_TOOL", "This tool is not registered.")
            self.trace.append("parse")
            if len(call.arguments_json.encode("utf-8")) > context.max_argument_bytes:
                raise ToolFailure("ARGUMENTS_TOO_LARGE", "Tool arguments exceed the byte limit.")
            try:
                arguments = json.loads(call.arguments_json)
            except ValueError:
                raise ToolFailure("INVALID_JSON", "Tool arguments must be valid JSON.") from None
            self.trace.append("validate")
            definition.validate(arguments)
            self.trace.append("authorize")
            if call.name not in context.allowed_tools:
                raise ToolFailure("TOOL_DENIED", "This tool is not allowed in the current context.")
            context.checkpoint()
            self.trace.append("execute")
            value = definition.handler(arguments, context)
            context.checkpoint()
            # This checks serializability, not a complete output schema.
            json.dumps(value, allow_nan=False)
            result = {"call_id": call.id, "tool": call.name, "ok": True, "value": value}
        except ToolFailure as error:
            result = {"call_id": call.id, "tool": call.name, "ok": False,
                      "error": {"code": error.code, "message": str(error)}}
        except Exception:
            # Unexpected bugs retain a developer traceback instead of leaking it to the model.
            LOGGER.exception("Tool %s failed (call %s)", call.name, call.id)
            result = {"call_id": call.id, "tool": call.name, "ok": False,
                      "error": {"code": "INTERNAL_ERROR", "message": "The tool failed internally."}}
        self.trace.append("result")
        return result


def read_registered_text(file_name, context):
    """Enforce resource policy at the file-reading operation, including direct calls."""
    context.checkpoint()
    if file_name not in context.files:
        raise ToolFailure("FILE_DENIED", "This file name is not available in the current context.")
    # The host owns this map. Model text is never joined to a host directory.
    target = context.files[file_name]
    try:
        data = bytearray()
        with target.open("rb") as stream:
            while True:
                context.checkpoint()
                chunk = stream.read(min(1024, context.max_read_bytes + 1 - len(data)))
                context.checkpoint()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > context.max_read_bytes:
                    raise ToolFailure("FILE_TOO_LARGE", "The file exceeds this call's read limit.")
        text = bytes(data).decode("utf-8")
    except FileNotFoundError:
        raise ToolFailure("FILE_NOT_FOUND", "The registered file no longer exists.") from None
    except UnicodeError:
        raise ToolFailure("NOT_UTF8", "The registered file is not valid UTF-8 text.") from None
    except OSError:
        raise ToolFailure("FILE_IO_ERROR", "The registered file could not be read.") from None
    return {"file": file_name, "content": text, "bytes_read": len(data)}


def make_runtime():
    runtime = ToolRuntime()
    runtime.register(ToolDefinition(
        name="read_file",
        description="Read a UTF-8 file by its host-registered logical name.",
        parameters=(StringParameter("path", "Logical name of the registered file."),),
        handler=lambda args, context: read_registered_text(args["path"], context),
    ))
    return runtime


def fixture_context(**overrides):
    values = {"allowed_tools": frozenset({"read_file"}),
              "files": {"package.json": Path(__file__).resolve().parent / "fixture" / "package.json"}}
    return ToolContext(**(values | overrides))


def main():
    runtime = make_runtime()
    print("OFFLINE TOOL EXECUTOR: no model, no network, no file mutations.")
    print("Catalog:", json.dumps(runtime.catalog(fixture_context()), indent=2))
    scenarios = [
        ("success", "read_file", '{"path":"package.json"}', fixture_context()),
        ("invalid JSON", "read_file", "{broken", fixture_context()),
        ("wrong argument type", "read_file", '{"path":123}', fixture_context()),
        ("unknown tool", "run_shell", "{}", fixture_context()),
        ("disabled tool", "read_file", '{"path":"package.json"}',
         fixture_context(allowed_tools=frozenset())),
        ("unregistered file", "read_file", '{"path":"../private.txt"}', fixture_context()),
        ("deadline reached", "read_file", '{"path":"package.json"}',
         fixture_context(deadline=monotonic() - 1)),
        ("read limit", "read_file", '{"path":"package.json"}', fixture_context(max_read_bytes=8)),
    ]
    for index, (label, name, arguments, context) in enumerate(scenarios, 1):
        result = runtime.execute(ToolCall(f"demo_{index}", name, arguments), context)
        print(f"\n{label}: {' -> '.join(runtime.trace)}")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
