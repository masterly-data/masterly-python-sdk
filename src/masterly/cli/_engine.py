"""``masterly config migrate``: move a configuration repository to a newer schema version as an
edit the customer reviews rather than authors (ADR 0082, rule 5).

The command reads the repository's declared schema version (``masterly.yaml``; none means the
version that predates the declaration), applies every migration between it and the target to
each configuration document the repository holds, writes the declaration, and prints the
unified diff of what it changed. The working tree is the customer's; the commit is theirs.

Two things about how it edits:

- **Only the documents a step reaches are rewritten, and only when the step changed them.**
  A file no step touches is not reparsed and rewritten — a reserialised YAML file is a diff
  with no change in it, which is exactly the noise a reviewer cannot review past.
- **A rewritten document keeps its comments, key order, quoting and indentation style.** The
  round-trip parser (``ruamel.yaml``) preserves the first three; the indentation of block
  sequences — ``- item`` flush under its key, or indented two spaces as the product writes a
  pipeline file — is read off the file itself before it is re-emitted.
"""

from __future__ import annotations

import difflib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from masterly.cli import _schema
from masterly.cli._schema import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION_FILE,
    SCHEMA_VERSION_KEY,
    UNDECLARED_SCHEMA_VERSION,
    DropKey,
    Migration,
    RenameKey,
    Rewrite,
)


class MigrateError(Exception):
    """A reason the repository cannot be migrated as asked, in a sentence for the terminal."""


@dataclass(frozen=True)
class Change:
    """One file the migration rewrites: its path, the text before and after, and the notes the
    steps left about what they could not do mechanically."""

    path: str
    before: str | None
    after: str
    notes: tuple[str, ...] = ()

    def unified_diff(self) -> str:
        """The change as ``git diff`` would show it, so the terminal and the commit agree."""
        before = self.before.splitlines(keepends=True) if self.before is not None else []
        lines = difflib.unified_diff(
            before,
            self.after.splitlines(keepends=True),
            fromfile=f"a/{self.path}" if self.before is not None else "/dev/null",
            tofile=f"b/{self.path}",
        )
        return "".join(lines)


@dataclass(frozen=True)
class Plan:
    """What a migration would do: the versions it moves between and the files it rewrites."""

    root: Path
    from_version: int
    to_version: int
    declared: bool
    changes: tuple[Change, ...] = field(default_factory=tuple)

    @property
    def is_noop(self) -> bool:
        return not self.changes


def plan(root: Path, *, to_version: int | None = None) -> Plan:
    """Compute the migration of the repository at ``root`` to ``to_version`` (the newest this
    client knows by default) without writing anything."""
    yaml = _load_yaml()
    root = root.resolve()
    if not root.is_dir():
        raise MigrateError(f"{root} is not a directory")
    declared_value, declared = _declared_version(root, yaml)
    from_version = _resolve_version(declared_value)
    target = CURRENT_SCHEMA_VERSION if to_version is None else to_version
    if target > CURRENT_SCHEMA_VERSION:
        raise MigrateError(
            f"this client migrates up to schema version {CURRENT_SCHEMA_VERSION}; "
            f"{target} needs a newer release — pip install -U 'masterly[cli]'"
        )
    if target < from_version:
        raise MigrateError(
            f"the repository is on schema version {from_version} and a migration only moves "
            f"forward; schema version {target} is behind it"
        )
    documents = _documents(root)
    if not documents and not declared and not (root / "modules.yaml").is_file():
        raise MigrateError(
            f"{root} does not look like a Masterly configuration repository: no "
            f"{SCHEMA_VERSION_FILE}, no modules.yaml and no configuration directory"
        )
    try:
        migrations = _schema.migrations_between(from_version, target)
    except LookupError as exc:
        raise MigrateError(str(exc)) from exc

    changes: list[Change] = []
    for relative, section in documents:
        steps = [step for m in migrations for step in m.steps if step.section == section]
        if not steps:
            continue
        before = (root / relative).read_text(encoding="utf-8")
        after, notes = _apply_steps(before, steps, yaml, relative)
        if after != before or notes:
            changes.append(Change(relative, before, after, tuple(notes)))
    if target != from_version or not declared:
        changes.append(_declaration_change(root, target, yaml))
    return Plan(root, from_version, target, declared, tuple(changes))


def write(plan_: Plan) -> None:
    """Perform the plan: write every changed file. The caller reviews and commits."""
    for change in plan_.changes:
        (plan_.root / change.path).write_text(change.after, encoding="utf-8")


def describe(plan_: Plan, migrations: list[Migration] | None = None) -> str:
    """The summary printed under the diff: what moved, what each step was for, and every note
    a step left about something it did not do."""
    if plan_.is_noop:
        return (
            f"{SCHEMA_VERSION_FILE} declares schema version {plan_.from_version}; "
            "nothing to migrate."
        )
    moved = (
        f"schema version {plan_.from_version} -> {plan_.to_version}"
        if plan_.to_version != plan_.from_version
        else f"schema version {plan_.to_version}, now declared in {SCHEMA_VERSION_FILE}"
    )
    lines = [f"{moved}: {len(plan_.changes)} file(s) changed"]
    steps = [
        step
        for m in (migrations or _schema.migrations_between(plan_.from_version, plan_.to_version))
        for step in m.steps
    ]
    for step in steps:
        lines.append(f"  - {step.id}: {step.why}")
    notes = [(change.path, note) for change in plan_.changes for note in change.notes]
    if notes:
        lines.append("Not done mechanically — review by hand:")
        lines.extend(f"  {path}: {note}" for path, note in notes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------


def _load_yaml() -> Any:
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover - exercised by the console script
        raise MigrateError(
            "masterly config migrate needs the cli extra: pip install 'masterly[cli]'"
        ) from exc
    yaml = YAML()
    yaml.preserve_quotes = True
    # ruamel folds any line past `width` (default 80) when it dumps, so a long description or
    # flow sequence the migration never touched would come back re-wrapped and show in the
    # diff. Effectively unbounded: a line is emitted as wide as it was read.
    yaml.width = 1 << 20
    return yaml


def _declared_version(root: Path, yaml: Any) -> tuple[Any, bool]:
    """(the declared value, whether the file is there). The value is raw, judged next."""
    path = root / SCHEMA_VERSION_FILE
    if not path.is_file():
        return None, False
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - every parser fault reads the same to the user
        raise MigrateError(f"{SCHEMA_VERSION_FILE} does not parse: {exc}") from exc
    if loaded is None:
        return None, True
    if not hasattr(loaded, "get"):
        raise MigrateError(f"{SCHEMA_VERSION_FILE} must be a mapping with {SCHEMA_VERSION_KEY}")
    return loaded.get(SCHEMA_VERSION_KEY), True


def _resolve_version(declared: Any) -> int:
    if declared is None:
        return UNDECLARED_SCHEMA_VERSION
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise MigrateError(
            f"{SCHEMA_VERSION_FILE}: {SCHEMA_VERSION_KEY} must be an integer, got {declared!r}"
        )
    if declared < 1:
        raise MigrateError(
            f"{SCHEMA_VERSION_FILE}: {SCHEMA_VERSION_KEY} {declared} is not a schema version"
        )
    if declared > CURRENT_SCHEMA_VERSION:
        raise MigrateError(
            f"{SCHEMA_VERSION_FILE} declares schema version {declared}, newer than this client "
            f"understands ({CURRENT_SCHEMA_VERSION}) — pip install -U 'masterly[cli]'"
        )
    return declared


def _documents(root: Path) -> list[tuple[str, str]]:
    """Every configuration document under ``root`` as (relative path, section), sorted. Only
    the layout's own directories are walked, so the customer's other files are never read."""
    found: list[tuple[str, str]] = []
    for prefix, section in _schema.PREFIX_TO_SECTION:
        directory = root / prefix.rstrip("/")
        if not directory.is_dir():
            continue
        for file in sorted(directory.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(root).as_posix()
            if _schema.section_of(relative) == section:
                found.append((relative, section))
    return sorted(set(found))


def _apply_steps(
    text: str, steps: list[_schema.Step], yaml: Any, path: str
) -> tuple[str, list[str]]:
    try:
        document = yaml.load(text)
    except Exception as exc:  # noqa: BLE001 - every parser fault reads the same to the user
        raise MigrateError(f"{path} does not parse as YAML: {exc}") from exc
    if document is None or not hasattr(document, "keys"):
        return text, []  # an empty or non-mapping file is the linter's to report
    notes: list[str] = []
    changed = False
    for step in steps:
        if isinstance(step, DropKey):
            for holder, key in _schema.holders(document, step.path):
                del holder[key]
                changed = True
        elif isinstance(step, RenameKey):
            for holder, key in _schema.holders(document, step.path):
                _rename_in_place(holder, key, step.to)
                changed = True
        elif isinstance(step, Rewrite):
            before = _dump(document, yaml, text)
            notes.extend(step.apply(document))
            changed = changed or _dump(document, yaml, text) != before
    if not changed:
        return text, notes
    return _dump(document, yaml, text), notes


def _rename_in_place(holder: Any, key: str, to: str) -> None:
    """Rename ``key`` to ``to`` keeping its position among the holder's keys — a reviewer reads
    a renamed line, not a line removed here and added at the bottom."""
    if to in holder:
        del holder[key]  # the new key is already there: the old one simply goes
        return
    if hasattr(holder, "insert"):
        position = list(holder.keys()).index(key)
        value = holder.pop(key)
        holder.insert(position, to, value)
    else:  # pragma: no cover - a plain dict never reaches here from the round-trip loader
        holder[to] = holder.pop(key)


_SEQUENCE_STYLE = re.compile(r"^( *)[^ #\n][^\n]*:[ \t]*(?:#[^\n]*)?\n( *)- +(\S)", re.MULTILINE)


def _dump(document: Any, yaml: Any, original: str) -> str:
    """Serialise ``document`` in the indentation style ``original`` was written in."""
    match = _SEQUENCE_STYLE.search(original)
    if match is not None:
        # All three columns relative to the key the sequence hangs under: where the dash
        # sits (`offset`) and where the item's content starts (`sequence`), in ruamel's terms.
        key_indent, dash_indent = len(match.group(1)), len(match.group(2))
        offset = dash_indent - key_indent
        content = match.start(3) - match.start(2) - key_indent
        yaml.indent(mapping=2, sequence=max(content, offset + 2), offset=max(offset, 0))
    else:
        yaml.indent(mapping=2, sequence=2, offset=0)
    buffer = io.StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


def _declaration_change(root: Path, version: int, yaml: Any) -> Change:
    path = root / SCHEMA_VERSION_FILE
    if not path.is_file():
        return Change(SCHEMA_VERSION_FILE, None, f"{SCHEMA_VERSION_KEY}: {version}\n")
    before = path.read_text(encoding="utf-8")
    loaded = yaml.load(before)
    if loaded is None or not hasattr(loaded, "keys"):
        return Change(SCHEMA_VERSION_FILE, before, f"{SCHEMA_VERSION_KEY}: {version}\n")
    loaded[SCHEMA_VERSION_KEY] = version
    return Change(SCHEMA_VERSION_FILE, before, _dump(loaded, yaml, before))
