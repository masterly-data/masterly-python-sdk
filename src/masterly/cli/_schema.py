"""The configuration repository's shape, as the migrate command knows it: the layout, the
schema version and its declaration file, and the table of migrations between versions.

This mirrors the server's own knowledge (`modules/configuration/evolution.py` and the GitOps
routing table in `masterly-application-backend`). Both sides read the same two things — the
repository layout and the deprecation path language — and each deprecation the server
announces has a step here under the **same id**, so a linter warning that ends in
``(data_models.attribute.historized)`` names the step that performs the edit.

Why a table shipped in the client rather than rules fetched from the server: a migration is an
edit to a working tree, made before a commit, by someone who may be offline and whose install
may be a release behind. The table is what the linter's warning told them to run, and it needs
no connection and no token. The cost is that the two tables can drift; the server's registry
test and ``tests/test_cli_migrate.py`` each hold their side to the contract, and the ids are
the join.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

#: The newest schema version this client can migrate a repository to. Moves together with the
#: table below, and never ahead of the server release that validates that version.
CURRENT_SCHEMA_VERSION = 1

#: The version a repository is on when it declares none: the one that predates the declaration.
UNDECLARED_SCHEMA_VERSION = 1

#: The declaration, at the repository root beside ``modules.yaml``.
SCHEMA_VERSION_FILE = "masterly.yaml"
SCHEMA_VERSION_KEY = "schema_version"

#: The root files Masterly writes; everything else at the root is the customer's.
ROOT_CONFIG_FILES = frozenset({SCHEMA_VERSION_FILE, "modules.yaml"})

#: Path prefix -> bundle section: the GitOps repository layout, longer prefixes first. The
#: section names are the server's (``data_models``, not ``data-models``), because that is what
#: the deprecation ids and the linter's messages use.
#:
#: Code lists have two directories. ``code-lists/`` is their name since the reference list was
#: renamed the code list (ADR 0146); ``reference/`` is the old name, which the server reads
#: until its dated cutover, so a repository laid out either way is still walked. Both route to
#: the one section the server keeps for them, ``reference_lists``: its alias for the new
#: directory names that section, and so do its deprecation ids for the renames.
PREFIX_TO_SECTION: tuple[tuple[str, str], ...] = (
    ("structure-fields/", "structure_fields"),
    ("workspaces/", "workspaces"),
    ("domains/", "domains"),
    ("data-models/", "data_models"),
    ("sources/", "sources"),
    ("data-products/", "data_products"),
    ("pipelines/", "pipelines"),
    ("access/policies/", "access_policies"),
    ("rules/survivorship/", "survivorship"),
    ("rules/record-layout/", "record_layouts"),
    ("rules/match/", "match_configs"),
    ("rules/data-quality/", "dq_rules"),
    ("rules/golden-profiles/", "golden_profiles"),
    ("code-lists/", "reference_lists"),
    ("reference/", "reference_lists"),
    ("glossary/", "glossary_terms"),
    ("hierarchies/", "hierarchies"),
    ("settings/", "environment_settings"),
)

CONFIG_SUFFIXES = (".yaml", ".yml")


def section_of(path: str) -> str | None:
    """The bundle section a repository-relative path belongs to, or None when it is not a
    configuration document (a README, a workflow, a file outside the layout)."""
    if path in ROOT_CONFIG_FILES or not path.endswith(CONFIG_SUFFIXES):
        return None
    return next((section for prefix, section in PREFIX_TO_SECTION if path.startswith(prefix)), None)


# ---------------------------------------------------------------------------------------------
# The path language — the same one the server's deprecation registry uses: dotted keys, and
# ``[]`` after a key for "every element of that list". The shape a step edits is the final key.

_PATH_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)(\[\])?$")


def parse_path(path: str) -> tuple[tuple[str, bool], ...]:
    """``path`` as (key, every-element-of-the-list) segments; ValueError on a path outside the
    language, so a malformed table entry fails the table's own test rather than a migration."""
    segments: list[tuple[str, bool]] = []
    for raw in path.split("."):
        match = _PATH_SEGMENT.match(raw)
        if match is None:
            raise ValueError(f"'{path}' is not a deprecation path: segment '{raw}'")
        segments.append((match.group(1), match.group(2) is not None))
    if segments[-1][1]:
        raise ValueError(f"'{path}' is not a deprecation path: the shape is a key, not a list")
    return tuple(segments)


def holders(document: Any, path: str) -> list[tuple[Any, str]]:
    """Every (mapping, key) in ``document`` where the shape ``path`` names is present — the
    mapping that holds the final key, so a step can drop or rename it in place."""
    found: list[tuple[Any, str]] = []
    _walk(document, parse_path(path), found)
    return found


def _walk(value: Any, segments: tuple[tuple[str, bool], ...], found: list[tuple[Any, str]]) -> None:
    key, each = segments[0]
    if not hasattr(value, "keys") or key not in value:
        return
    if len(segments) == 1:
        found.append((value, key))
        return
    inner = value[key]
    if each:
        if isinstance(inner, list):
            for item in inner:
                _walk(item, segments[1:], found)
    else:
        _walk(inner, segments[1:], found)


# ---------------------------------------------------------------------------------------------
# The steps a migration is made of. Each carries the id of the server-side deprecation it
# performs, the section whose files it visits, and a sentence for the summary.


@dataclass(frozen=True)
class DropKey:
    """Remove the key ``path`` names wherever a record carries it."""

    id: str
    section: str
    path: str
    why: str


@dataclass(frozen=True)
class RenameKey:
    """Rename the key ``path`` names to ``to``, in place, keeping its value and its position."""

    id: str
    section: str
    path: str
    to: str
    why: str


@dataclass(frozen=True)
class Rewrite:
    """An edit the two declarative steps cannot express — one that moves a value somewhere its
    shape has to be worked out per record. ``apply`` edits the record in place and returns a
    note per thing it could **not** place, each of which the command reports beside the diff
    rather than guessing (ADR 0095 §4 is the first such rule: a retired ``relationships`` entry
    moves onto the one attribute that matches it, and an entry that matches none is reported)."""

    id: str
    section: str
    why: str
    apply: Callable[[Any], list[str]]


Step = DropKey | RenameKey | Rewrite


@dataclass(frozen=True)
class Migration:
    """The mechanical edit from ``to_version - 1`` to ``to_version``: the steps, applied to
    every configuration document in the repository, in order."""

    to_version: int
    steps: tuple[Step, ...]


#: One entry per schema version after the first, contiguous — ``tests/test_cli_migrate.py``
#: holds the table to that. Empty today: the current schema is version 1, retrofitted as the
#: first version of the scheme with no change to its shape, and the first entries are the
#: fields the product retired in place ahead of this command (``historized``,
#: ``definition.relationships``).
MIGRATIONS: tuple[Migration, ...] = ()


# ---------------------------------------------------------------------------------------------
# Renames. A rename is not a schema version: both names stay valid and the server only warns
# on the old one until a dated cutover (ADR 0082). So it lives in its own table, applies to a
# repository at any declared version, and never moves the version or touches ``masterly.yaml``.

#: The configuration format's announcement window: twelve months, the API's.
RENAME_WINDOW_MONTHS = 12

_CODE_LISTS_ANNOUNCED = date(2026, 11, 15)


@dataclass(frozen=True)
class RenameRule:
    """One announced rename, under the id the server's linter ends its warning with.

    ``shape`` is what the old name is: ``section`` (the bundle section's own key),
    ``path_prefix`` (the repository directory the section's files live under) or ``field`` (a
    key inside a record, in any dict that carries every ``where`` pair, at any depth)."""

    id: str
    section: str
    shape: Literal["section", "path_prefix", "field"]
    old: str
    new: str
    announced: date
    why: str
    where: tuple[tuple[str, str], ...] = ()
    #: Why a working tree has nothing for the command to edit, when that is so.
    not_applicable: str | None = None

    @property
    def cutover(self) -> date:
        """The last day export writes the old name; from the day after, it writes the new one."""
        month = self.announced.month - 1 + RENAME_WINDOW_MONTHS
        return self.announced.replace(year=self.announced.year + month // 12, month=month % 12 + 1)


#: Every rename the server announces (``evolution.RENAMES`` in ``masterly-application-backend``,
#: as of its commit 7f483dde11), under the same ids. ``tests/test_cli_migrate.py`` holds the id
#: set to the server's.
RENAMES: tuple[RenameRule, ...] = (
    RenameRule(
        id="reference_lists.code_lists",
        section="reference_lists",
        shape="section",
        old="reference_lists",
        new="code_lists",
        announced=_CODE_LISTS_ANNOUNCED,
        why="the bundle section is named for what it holds",
        not_applicable=(
            "it renames a bundle section's key, and a GitOps working tree has no such key — "
            "its sections are directories, which reference_lists.code-lists moves"
        ),
    ),
    RenameRule(
        id="reference_lists.code-lists",
        section="reference_lists",
        shape="path_prefix",
        old="reference/",
        new="code-lists/",
        announced=_CODE_LISTS_ANNOUNCED,
        why="the reference list is the code list; its files move to code-lists/",
    ),
    RenameRule(
        id="dq_rules.code_list",
        section="dq_rules",
        shape="field",
        old="reference_list",
        new="code_list",
        announced=_CODE_LISTS_ANNOUNCED,
        why="an in_reference rule names its code list",
        where=(("kind", "in_reference"),),
    ),
)


def migrations_between(from_version: int, to_version: int) -> list[Migration]:
    """The migrations that take a repository from ``from_version`` to ``to_version``, in order.
    Raises LookupError when the table has a gap — every version in between must have one."""
    by_version = {migration.to_version: migration for migration in MIGRATIONS}
    path: list[Migration] = []
    for version in range(from_version + 1, to_version + 1):
        if version not in by_version:
            raise LookupError(f"this client has no migration to schema version {version}")
        path.append(by_version[version])
    return path
