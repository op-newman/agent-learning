"""Offline lesson 8: how a dsh profile is assembled from bundles and patches.

This module models the configuration plumbing that turns a named profile into a
concrete plugin tree. It reproduces four rules that decide whether an
installation starts at all:

* a bundle is an ordered list of patch documents, and a profile is an ordered
  list of bundles plus its own ``cordis.patch.yml``;
* the layers stack as bundles -> profile patch -> home patch -> each ``--patch``
  overlay in argv order, with the last write winning per row;
* a patch that targets a row by ``id`` replaces that row's whole ``config``
  instead of deep-merging into it; and
* two active rows that register the same service make the profile fail to load.

Everything here is deterministic, in memory, and offline. Names mirror the real
DeepSeek Harness packages, but the rows are teaching fixtures, not that tree.
"""

from dataclasses import dataclass, field
from typing import Iterable, Mapping

# Platform gates in the real patches are ``!!js process.platform === ...``
# expressions. The model uses a plain allow-list instead.
POSIX = ("linux", "darwin")
WINDOWS = "win32"

HOME_LAYER = "$DSH_HOME/cordis.patch.yml"

# Why a row is currently switched off.
OFF_PLATFORM = "platform"
OFF_EXPLICIT = "explicit"


class AssemblyError(Exception):
    """A load-time failure with a stable, machine-readable code."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass
class Row:
    """One entry in the assembled plugin tree."""

    id: str
    name: str
    config: dict = field(default_factory=dict)
    provides: tuple = ()
    requires: tuple = ()
    platforms: tuple | None = None  # None means "no gate"
    source: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    history: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Only an active row registers its service and loads its module."""

        return not self.disabled

    def apply_platform_gate(self, platform: str) -> None:
        """A gated row is off unless the host platform is in its allow-list.

        Committing an explicit ``disabled`` value in a patch clears the gate for
        that row, exactly like the real ``!!js`` expression being overwritten.
        """

        if self.platforms is None:
            return
        self.disabled = platform not in self.platforms
        self.disabled_reason = "" if not self.disabled else OFF_PLATFORM

    def restate(self, entry: Mapping, layer: str) -> None:
        """Apply an id-addressed patch entry: replace, never merge."""

        if "name" in entry:
            self.name = entry["name"]
        if "config" in entry:
            # The whole block is replaced. A deep merge here would silently keep
            # fields the author meant to drop, so the real patch layer does not
            # merge and this model does not either.
            self.config = dict(entry["config"] or {})
        if "platforms" in entry:
            value = entry["platforms"]
            self.platforms = None if value is None else tuple(value)
        if "disabled" in entry:
            self.disabled = bool(entry["disabled"])
            self.disabled_reason = "" if not self.disabled else OFF_EXPLICIT
        if "provides" in entry:
            self.provides = tuple(entry["provides"])
        if "requires" in entry:
            self.requires = tuple(entry["requires"])
        self.source = layer
        self.history.append(layer)


def _insert_row(raw: Mapping, layer: str, platform: str) -> Row:
    if "id" not in raw or "name" not in raw:
        raise AssemblyError("INVALID_PATCH", f"{layer}: insert needs 'id' and 'name'")

    value = raw.get("platforms")
    row = Row(
        id=raw["id"],
        name=raw["name"],
        config=dict(raw.get("config") or {}),
        provides=tuple(raw.get("provides", ())),
        requires=tuple(raw.get("requires", ())),
        platforms=None if value is None else tuple(value),
        source=layer,
        disabled=bool(raw.get("disabled", False)),
        disabled_reason=OFF_EXPLICIT if raw.get("disabled") else "",
    )
    row.apply_platform_gate(platform)
    row.history.append(layer)
    return row


@dataclass
class Tree:
    """An ordered map of row id -> Row, plus the layers that produced it."""

    rows: dict = field(default_factory=dict)
    layers: list = field(default_factory=list)

    def active_ids(self) -> tuple:
        return tuple(row.id for row in self.rows.values() if row.active)

    def get(self, row_id: str) -> Row:
        return self.rows[row_id]


def apply_patch(tree: Tree, patch_doc: Iterable, layer: str, platform: str) -> None:
    """Apply one patch document.

    Two entry shapes exist, matching the YAML entry-list dialect: ``insert``
    adds new rows, and a bare entry with ``id`` patches an existing row.
    """

    for entry in patch_doc:
        if not isinstance(entry, Mapping):
            raise AssemblyError("INVALID_PATCH", f"{layer}: entry must be a mapping")
        if "insert" in entry:
            for raw in entry["insert"]:
                row = _insert_row(raw, layer, platform)
                if row.id in tree.rows:
                    raise AssemblyError(
                        "DUPLICATE_ENTRY", f"{layer}: insert reuses id {row.id!r}"
                    )
                tree.rows[row.id] = row
        elif "id" in entry:
            row_id = entry["id"]
            existing = tree.rows.get(row_id)
            if existing is None:
                raise AssemblyError(
                    "UNKNOWN_ENTRY", f"{layer}: patch targets unknown id {row_id!r}"
                )
            existing.restate(entry, layer)
        else:
            raise AssemblyError(
                "INVALID_PATCH", f"{layer}: entry needs 'insert' or 'id'"
            )
    tree.layers.append(layer)


@dataclass
class Bundle:
    """A distributable patch layer: ``dsh.bundle.patch`` as a file list."""

    name: str
    patch_files: list

    def __init__(self, name: str, patch_files: Iterable):
        self.name = name
        self.patch_files = list(patch_files)


@dataclass
class Profile:
    """A named assembly: ``dsh.profile.bundles`` plus its own patch."""

    name: str
    bundles: list
    patch: list

    def __init__(self, name: str, bundles: Iterable, patch: Iterable = ()):
        self.name = name
        self.bundles = list(bundles)
        self.patch = list(patch)


class Registry:
    """Resolves bundle names the way the launcher resolves installed packages."""

    def __init__(self, bundles: Iterable = ()):
        self.bundles = {}
        for bundle in bundles:
            self.add(bundle)

    def add(self, bundle: Bundle) -> None:
        self.bundles[bundle.name] = bundle

    def resolve(self, name: str) -> Bundle:
        try:
            return self.bundles[name]
        except KeyError:
            # The real loader skips one unreadable bundle and reports it; a name
            # that a profile needed and the registry cannot supply is a failure.
            raise AssemblyError("BUNDLE_NOT_FOUND", f"no bundle named {name!r}") from None


def assemble(
    profile: Profile,
    registry: Registry,
    platform: str = "linux",
    home_patch: Iterable = (),
    overlays: Iterable = (),
) -> Tree:
    """Build the effective tree in the documented layer order."""

    tree = Tree()
    for bundle_name in profile.bundles:
        bundle = registry.resolve(bundle_name)
        for index, patch_doc in enumerate(bundle.patch_files):
            apply_patch(tree, patch_doc, f"{bundle.name}#{index}", platform)
    apply_patch(tree, profile.patch, f"profile:{profile.name}", platform)
    if home_patch:
        apply_patch(tree, home_patch, HOME_LAYER, platform)
    for index, overlay in enumerate(overlays):
        apply_patch(tree, overlay, f"--patch[{index}]", platform)
    return tree


@dataclass
class Report:
    """The result of validating an assembled tree."""

    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate(tree: Tree) -> Report:
    """Check singleton services and unmet dependencies.

    A service may have at most one active provider: the container rejects a
    second registration of the same key, which is why mounting two filesystem
    providers or two shell stacks fails the whole profile. A missing service is
    not fatal, because the dependent row can simply wait for it, so it is only a
    warning.
    """

    report = Report()
    providers: dict = {}
    for row in tree.rows.values():
        if not row.active:
            continue
        for service in row.provides:
            providers.setdefault(service, []).append(row.id)
    for service, row_ids in providers.items():
        if len(row_ids) > 1:
            report.problems.append(
                AssemblyError(
                    "DUPLICATE_SERVICE",
                    f"service {service!r} is provided by {row_ids}; only one may be active",
                )
            )
    provided = set(providers)
    for row in tree.rows.values():
        if not row.active:
            continue
        for service in row.requires:
            if service not in provided:
                report.warnings.append(
                    f"{row.id} waits for missing service {service!r}"
                )
    return report


def dump_config(tree: Tree) -> str:
    """Render the effective tree, annotating the layer each row came from."""

    lines = []
    for row in tree.rows.values():
        reason = f" ({row.disabled_reason})" if row.disabled_reason else ""
        mark = " [off]" if row.disabled else ""
        lines.append(f"- id: {row.id}   (from {row.source}){mark}{reason}")
        lines.append(f"    name: {row.name}")
        if row.provides:
            lines.append(f"    provides: {list(row.provides)}")
        if row.config:
            pairs = ", ".join(f"{key}={value}" for key, value in row.config.items())
            lines.append(f"    config: {{{pairs}}}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Teaching fixtures
# ---------------------------------------------------------------------------

BASE_BUNDLE = Bundle(
    "@deepseek-ai/dsh-base",
    [
        # The shared core applied as one insert over the empty profile root.
        [
            {"insert": [
                {"id": "tools", "name": "@deepseek-ai/dsh-tools"},
                {"id": "llm-deepseek", "name": "@deepseek-ai/dsh-llm-deepseek-api-key"},
                {"id": "session-persistence-jsonl",
                 "name": "@deepseek-ai/dsh-session-persistence-jsonl",
                 "config": {"root": "$DSH_HOME/sessions"}},
                {"id": "agent-default-model",
                 "name": "@deepseek-ai/dsh-agent-default-model",
                 "config": {"provider": "deepseek-official", "model": "deepseek-flash"}},
                {"id": "fs-sandbox", "name": "@deepseek-ai/dsh-fs-sandbox",
                 "provides": ["fs"]},
                {"id": "tool-read", "name": "@deepseek-ai/dsh-tool-read",
                 "config": {"maxOutputChars": 16000}},
                {"id": "tool-write", "name": "@deepseek-ai/dsh-tool-write"},
                {"id": "tool-edit", "name": "@deepseek-ai/dsh-tool-edit"},
                # Two shell rows gated by platform: exactly one is ever active.
                {"id": "bash-sandbox", "name": "@deepseek-ai/dsh-sandbox-local",
                 "platforms": list(POSIX), "provides": ["shell"]},
                {"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash",
                 "platforms": list(POSIX), "requires": ["shell"]},
                {"id": "pwsh-sandbox", "name": "@deepseek-ai/dsh-sandbox-windows-acl",
                 "platforms": [WINDOWS], "provides": ["shell"]},
                {"id": "tool-pwsh", "name": "@deepseek-ai/dsh-tool-pwsh",
                 "platforms": [WINDOWS], "requires": ["shell"]},
            ]},
        ],
    ],
)

WEB_APP_BUNDLE = Bundle(
    "@deepseek-ai/dsh-web-app",
    [
        [
            {"insert": [
                {"id": "webserver", "name": "@deepseek-ai/dsh-host-webserver"},
                {"id": "web-frontend", "name": "@deepseek-ai/dsh-web-frontend"},
                {"id": "plan-mode", "name": "@deepseek-ai/dsh-plan-mode"},
            ]},
        ],
    ],
)

SDK_MINIMAL_BUNDLE = Bundle(
    "@deepseek-ai/dsh-sdk-minimal",
    [
        # A standalone tree: it never names dsh-base, so nothing above applies.
        [
            {"insert": [
                {"id": "sdk-jsonrpc-server", "name": "@deepseek-ai/dsh-sdk-app"},
                {"id": "llm-deepseek", "name": "@deepseek-ai/dsh-llm-deepseek-api-key"},
                {"id": "session-persistence-jsonl",
                 "name": "@deepseek-ai/dsh-session-persistence-jsonl",
                 "config": {"root": "$DSH_HOME/sessions"}},
                {"id": "bash-sandbox", "name": "@deepseek-ai/dsh-sandbox-local",
                 "platforms": list(POSIX), "provides": ["shell"]},
                {"id": "tool-bash", "name": "@deepseek-ai/dsh-tool-bash",
                 "platforms": list(POSIX), "requires": ["shell"]},
                {"id": "pwsh-sandbox", "name": "@deepseek-ai/dsh-sandbox-windows-acl",
                 "platforms": [WINDOWS], "provides": ["shell"]},
                {"id": "tool-pwsh", "name": "@deepseek-ai/dsh-tool-pwsh",
                 "platforms": [WINDOWS], "requires": ["shell"]},
            ]},
        ],
    ],
)

DEFAULT_REGISTRY = Registry([BASE_BUNDLE, WEB_APP_BUNDLE, SDK_MINIMAL_BUNDLE])


def web_profile() -> Profile:
    return Profile(
        name="web",
        bundles=["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
        patch=[],
    )


def main() -> None:
    registry = DEFAULT_REGISTRY

    print("== layers stack in order: bundles, profile, home, overlays ==")
    home = [{"id": "agent-default-model",
             "config": {"provider": "deepseek-official", "model": "deepseek-pro"}}]
    overlay = [{"id": "agent-default-model",
                "config": {"provider": "deepseek-official", "model": "deepseek-flash"}}]
    tree = assemble(web_profile(), registry, platform="linux",
                    home_patch=home, overlays=[overlay])
    print("layers:", " -> ".join(tree.layers))
    model = tree.get("agent-default-model")
    print("agent-default-model.config:", model.config, "from", model.source)

    print("\n== a patch replaces the whole config, it does not merge ==")
    naive = Profile("naive", ["@deepseek-ai/dsh-base"],
                    patch=[{"id": "agent-default-model", "config": {"model": "deepseek-pro"}}])
    naive_tree = assemble(naive, registry, platform="linux")
    print("after naive patch:", naive_tree.get("agent-default-model").config)
    careful = Profile("careful", ["@deepseek-ai/dsh-base"],
                      patch=[{"id": "agent-default-model",
                              "config": {"provider": "deepseek-official",
                                         "model": "deepseek-pro"}}])
    careful_tree = assemble(careful, registry, platform="linux")
    print("after careful patch:", careful_tree.get("agent-default-model").config)

    print("\n== platform gating keeps exactly one shell stack ==")
    for platform in ("linux", "win32"):
        gated = assemble(web_profile(), registry, platform=platform)
        offered = tree.get if False else gated
        print(platform, "active shell rows:",
              [row_id for row_id in gated.active_ids() if row_id.endswith("sandbox")])
    # Committing ``disabled: false`` clears the gate, so both stacks go live at
    # once and the profile can no longer load.
    both = Profile("both-shells", ["@deepseek-ai/dsh-base"],
                   patch=[{"id": "pwsh-sandbox", "disabled": False},
                          {"id": "tool-pwsh", "disabled": False}])
    forced = assemble(both, registry, platform="linux")
    report = validate(forced)
    print("both shells ok?", report.ok, "-", report.problems[0].code)

    print("\n== a second filesystem provider fails the profile ==")
    duplicated = Profile(
        "dup-fs", ["@deepseek-ai/dsh-base"],
        patch=[{"insert": [{"id": "fs-local", "name": "@deepseek-ai/dsh-fs-local",
                            "provides": ["fs"]}]}],
    )
    dup_tree = assemble(duplicated, registry, platform="linux")
    dup_report = validate(dup_tree)
    print("dup fs ok?", dup_report.ok, "-", str(dup_report.problems[0]))

    print("\n== sdk-minimal is a standalone tree, not base plus extras ==")
    minimal = Profile("sdk-minimal", ["@deepseek-ai/dsh-sdk-minimal"], patch=[])
    minimal_tree = assemble(minimal, registry, platform="win32")
    print("sdk-minimal active rows:", ", ".join(minimal_tree.active_ids()))
    print("has base tool-read?", "tool-read" in minimal_tree.rows)

    print("\n== dump-config annotates the source layer ==")
    print(dump_config(assemble(web_profile(), registry, platform="win32")))


if __name__ == "__main__":
    main()