"""`masterly config migrate` (ADR 0082, rule 5): the mechanical edit as a diff the customer
reviews, over a customer-shaped multi-file repository.

The migration table is empty on `main` (the current schema is version 1 and nothing has been
removed from it yet), so the migration every test here runs is SYNTHETIC — a 1 -> 2 step set
patched into the table, shaped like the first real ones will be: the `historized` attribute
flag dropped, a key renamed, and a rewrite that reports what it cannot place.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from masterly.cli import _engine, _schema, main

# A repository as `config:export` writes it and a customer then edits: comments, quoted
# strings, flush block sequences in the sorted-key files, indented ones and one-line flow
# mappings in the pipeline file, and files that are the customer's own.
_FILES: dict[str, str] = {
    "README.md": "# Config for prod-eu\n",
    ".github/workflows/lint.yml": "on: pull_request\n",
    "modules.yaml": "activated:\n- platform\n- configuration\n- data-quality\n",
    "workspaces/customer-master.yaml": "description: null\nname: Customer master\n",
    "domains/customer-master/sales.yaml": (
        "defaults: null\ndescription: null\nname: Sales\nworkspace: Customer master\n"
    ),
    "data-models/account.yaml": (
        "# The account master — keep the keys in sync with the CRM extract.\n"
        "definition:\n"
        "  description: The legal entity we invoice, as the CRM extract names it, one row per"
        " registered company number and trading country.\n"
        "  attributes:\n"
        "  - name: account_number\n"
        "    type: string\n"
        "    historized: false   # retired flag, left by an old export\n"
        "  - name: name\n"
        "    type: string\n"
        "    historized: true\n"
        "  - name: 'segment'\n"
        "    type: enum\n"
        '    values: ["SMB", "Mid-market", "Enterprise", "Public sector", "Strategic", "Partner"]\n'
        "  keys:\n"
        "  - attributes: [account_number]\n"
        "    name: business\n"
        "domain:\n"
        "  name: Sales\n"
        "  workspace: Customer master\n"
        "kind: entity\n"
        "name: Account\n"
        "status: published\n"
    ),
    "data-models/contact.yaml": (
        "definition:\n"
        "  attributes:\n"
        "  - name: email\n"
        "    type: string\n"
        "domain:\n"
        "  name: Sales\n"
        "  workspace: Customer master\n"
        "kind: entity\n"
        "name: Contact\n"
        "status: draft\n"
    ),
    "data-models/costcenter.yaml": (
        "definition:\n"
        "  attributes:\n"
        "  - name: code\n"
        "    type: string\n"
        "  - name: parent\n"
        "    type: reference\n"
        "    target_model: CostCenter\n"
        "  relationships:\n"
        "  - kind: hierarchy\n"
        "    target_model: CostCenter\n"
        "  - kind: reference\n"
        "    target_model: Nowhere\n"
        "domain:\n"
        "  name: Sales\n"
        "  workspace: Customer master\n"
        "kind: entity\n"
        "name: CostCenter\n"
        "status: published\n"
    ),
    "sources/crm.yaml": (
        "mapping:\n"
        "  field_map:\n"
        "    ACCNO: account_number\n"
        "  source_key: account_number\n"
        "mode: push\n"
        "name: crm\n"
        "system_type: rest\n"
        "target_model: Account\n"
    ),
    "pipelines/nightly-master-refresh.yaml": (
        "name: Nightly master refresh\n"
        "description: Resolve, score and refresh the supplier directory every night, then scan"
        " it, so the morning extract carries a fresh score.\n"
        "definition:\n"
        "  triggers:\n"
        "    cron: 0 2 * * *\n"
        "  steps:\n"
        "    - id: resolve-suppliers\n"
        "      kind: golden.resolve\n"
        "      params: {model: Supplier}\n"
        "    - id: scan-suppliers\n"
        "      kind: dq.scan\n"
        "      params: {model: Supplier}\n"
        "      depends_on: [resolve-suppliers, resolve-customers, resolve-products,"
        " resolve-sites]\n"
    ),
    "rules/match/account.yaml": (
        "config:\n  rules:\n  - attributes: [account_number]\n    kind: exact\nmodel: Account\n"
    ),
    "settings/environment.yaml": (
        "document:\n  locale: en-GB\n  timezone: Europe/Stockholm\ntype: environment-settings\n"
    ),
}


def _write_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    for path, content in (files or _FILES).items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _move_relationships(record: Any) -> list[str]:
    """The ADR 0095 §4 rule, as a Rewrite: a retired `relationships` entry whose target matches
    exactly one reference attribute moves onto that attribute as `cardinality`; one that
    matches none is reported, not guessed."""
    definition = record.get("definition")
    if definition is None or "relationships" not in definition:
        return []
    notes: list[str] = []
    attributes = [a for a in definition.get("attributes", []) if a.get("type") == "reference"]
    for entry in definition["relationships"]:
        matches = [a for a in attributes if a.get("target_model") == entry.get("target_model")]
        if len(matches) == 1:
            matches[0]["cardinality"] = "many-to-one"
        else:
            notes.append(
                f"relationships entry targeting {entry.get('target_model')!r} matches "
                f"{len(matches)} reference attribute(s); place its cardinality by hand"
            )
    del definition["relationships"]
    return notes


_SYNTHETIC = _schema.Migration(
    to_version=2,
    steps=(
        _schema.DropKey(
            id="data_models.attribute.historized",
            section="data_models",
            path="definition.attributes[].historized",
            why="history is always kept, so the flag is dropped",
        ),
        _schema.RenameKey(
            id="pipelines.triggers.cron",
            section="pipelines",
            path="definition.triggers.cron",
            to="schedule",
            why="the trigger key is named for what it holds",
        ),
        _schema.Rewrite(
            id="data_models.definition.relationships",
            section="data_models",
            why="a relationship is its reference attribute's cardinality",
            apply=_move_relationships,
        ),
    ),
)


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> _schema.Migration:
    monkeypatch.setattr(_schema, "MIGRATIONS", (_SYNTHETIC,))
    monkeypatch.setattr(_schema, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(_engine, "CURRENT_SCHEMA_VERSION", 2)
    return _SYNTHETIC


def test_the_table_is_contiguous_and_each_step_names_a_section_and_a_path() -> None:
    """The ledger over the real table: one migration per version after the first, in order,
    every step on a known section with a path in the shared language, and the client never
    claims a version it has no migration to."""
    versions = [m.to_version for m in _schema.MIGRATIONS]
    assert versions == list(range(2, _schema.CURRENT_SCHEMA_VERSION + 1))
    sections = {section for _, section in _schema.PREFIX_TO_SECTION}
    for migration in _schema.MIGRATIONS:
        for step in migration.steps:
            assert step.section in sections, step.id
            assert step.id.startswith(f"{step.section}."), step.id
            if not isinstance(step, _schema.Rewrite):
                _schema.parse_path(step.path)
    assert _schema.migrations_between(1, _schema.CURRENT_SCHEMA_VERSION) == list(_schema.MIGRATIONS)
    with pytest.raises(LookupError):
        _schema.migrations_between(1, _schema.CURRENT_SCHEMA_VERSION + 1)
    for bad in ("", "a..b", "attributes[]", "a[0].b"):
        with pytest.raises(ValueError):
            _schema.parse_path(bad)


def test_the_layout_routes_exactly_the_files_the_server_reads() -> None:
    assert _schema.section_of("data-models/account.yaml") == "data_models"
    assert _schema.section_of("rules/golden-profiles/company/sales.yml") == "golden_profiles"
    assert _schema.section_of("access/policies/mask.yaml") == "access_policies"
    assert _schema.section_of("modules.yaml") is None
    assert _schema.section_of("masterly.yaml") is None
    assert _schema.section_of("data-models/notes.txt") is None
    assert _schema.section_of("src/data-models/x.yaml") is None


def test_migrate_produces_a_reviewable_diff_over_a_multi_file_repo(
    tmp_path: Path, announced: _schema.Migration, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command rewrites only the files a step changed, keeps every comment, quote, key order
    and indentation style around the edit, declares the new version, reports what it could not
    place, and leaves the customer's own files unread. The diff is `git diff`-shaped so what
    the terminal shows is what the commit will show."""
    root = _write_repo(tmp_path)
    before = {path: (root / path).read_text() for path in _FILES}
    # Lines over 80 characters (a description, a flow sequence) in the files a step rewrites:
    # the dump must not re-wrap them, or the diff carries lines no step edited.
    for path in ("data-models/account.yaml", "pipelines/nightly-master-refresh.yaml"):
        assert sum(len(line) > 80 for line in before[path].splitlines()) == 2, path

    assert main(["config", "migrate", str(root)]) == 0
    out = capsys.readouterr().out

    after = {path: (root / path).read_text() for path in _FILES}
    changed = {path for path in _FILES if after[path] != before[path]}
    assert changed == {
        "data-models/account.yaml",
        "data-models/costcenter.yaml",
        "pipelines/nightly-master-refresh.yaml",
    }
    assert (root / "masterly.yaml").read_text() == "schema_version: 2\n"

    # The dropped flag is the only change in the account file: comments, quotes, the flow
    # sequence and the flush block style all survive.
    assert after["data-models/account.yaml"] == before["data-models/account.yaml"].replace(
        "    historized: false   # retired flag, left by an old export\n", ""
    ).replace("    historized: true\n", "")
    # The pipeline file keeps its indented sequences and its one-line params; the renamed key
    # stays where it was.
    assert after["pipelines/nightly-master-refresh.yaml"] == before[
        "pipelines/nightly-master-refresh.yaml"
    ].replace("    cron: 0 2 * * *\n", "    schedule: 0 2 * * *\n")
    # The rewrite placed the entry it could, dropped the retired section, and reported the rest.
    costcenter = after["data-models/costcenter.yaml"]
    assert "relationships" not in costcenter
    assert "    target_model: CostCenter\n    cardinality: many-to-one\n" in costcenter
    assert "Not done mechanically" in out
    assert "data-models/costcenter.yaml: relationships entry targeting 'Nowhere'" in out

    # The diff is git-shaped, and names only what changed.
    assert "--- a/data-models/account.yaml\n+++ b/data-models/account.yaml\n" in out
    assert "-    historized: true\n" in out
    assert "--- /dev/null\n+++ b/masterly.yaml\n" in out
    assert "+schema_version: 2\n" in out
    assert "contact.yaml" not in out and "README" not in out
    assert "schema version 1 -> 2: 4 file(s) changed" in out
    assert "data_models.attribute.historized: history is always kept" in out

    # Idempotent: a second run has nothing to do, and says so.
    assert main(["config", "migrate", str(root)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out
    assert {path: (root / path).read_text() for path in _FILES} == after


def test_dry_run_and_check_write_nothing(tmp_path: Path, announced: _schema.Migration) -> None:
    root = _write_repo(tmp_path)
    before = {path: (root / path).read_text() for path in _FILES}
    assert main(["config", "migrate", str(root), "--dry-run"]) == 0
    assert main(["config", "migrate", str(root), "--check"]) == 1
    assert {path: (root / path).read_text() for path in _FILES} == before
    assert not (root / "masterly.yaml").exists()
    assert main(["config", "migrate", str(root)]) == 0
    assert main(["config", "migrate", str(root), "--check"]) == 0


def test_a_repository_without_a_declaration_gains_one_at_the_current_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the real (empty) table, the only edit a repository from before the declaration
    needs is the declaration itself — one new file, nothing else touched."""
    root = _write_repo(tmp_path)
    before = {path: (root / path).read_text() for path in _FILES}
    assert main(["config", "migrate", str(root)]) == 0
    out = capsys.readouterr().out
    assert (root / "masterly.yaml").read_text() == (
        f"schema_version: {_schema.CURRENT_SCHEMA_VERSION}\n"
    )
    assert {path: (root / path).read_text() for path in _FILES} == before
    assert out.count("+++ b/") == 1 and "now declared in masterly.yaml" in out


def test_a_declaration_with_comments_is_edited_in_place(
    tmp_path: Path, announced: _schema.Migration
) -> None:
    root = _write_repo(tmp_path)
    (root / "masterly.yaml").write_text(
        "# Which Masterly config schema this repo is on.\nschema_version: 1\n"
    )
    plan = _engine.plan(root)
    assert plan.from_version == 1 and plan.to_version == 2 and plan.declared
    declaration = next(c for c in plan.changes if c.path == "masterly.yaml")
    assert (
        declaration.after == "# Which Masterly config schema this repo is on.\nschema_version: 2\n"
    )


def test_refusals_name_the_reason(
    tmp_path: Path, announced: _schema.Migration, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _write_repo(tmp_path)
    # Newer than the client: the fix is an upgrade, not a downgrade of the repository.
    (root / "masterly.yaml").write_text("schema_version: 3\n")
    assert main(["config", "migrate", str(root)]) == 2
    assert "newer than this client understands" in capsys.readouterr().err
    # A migration never moves backwards.
    (root / "masterly.yaml").write_text("schema_version: 2\n")
    assert main(["config", "migrate", str(root), "--to", "1"]) == 2
    assert "only moves forward" in capsys.readouterr().err
    # A typo'd declaration is refused rather than read as some version.
    (root / "masterly.yaml").write_text("schema_version: yes\n")
    assert main(["config", "migrate", str(root)]) == 2
    assert "must be an integer" in capsys.readouterr().err
    # A target the client has no migration for.
    (root / "masterly.yaml").write_text("schema_version: 1\n")
    assert main(["config", "migrate", str(root), "--to", "5"]) == 2
    assert "needs a newer release" in capsys.readouterr().err
    # Not a configuration repository at all.
    assert main(["config", "migrate", str(tmp_path / "elsewhere")]) == 2
    assert "is not a directory" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["config", "migrate", str(empty)]) == 2
    assert "does not look like a Masterly configuration repository" in capsys.readouterr().err
    # A document that does not parse names its file.
    (root / "data-models" / "broken.yaml").write_text("x: [1, 2\n")
    assert main(["config", "migrate", str(root)]) == 2
    assert "data-models/broken.yaml does not parse" in capsys.readouterr().err


def test_a_step_that_changes_nothing_leaves_the_file_byte_identical(
    tmp_path: Path, announced: _schema.Migration
) -> None:
    """A document a step reaches but does not change is not reserialised — a file written in a
    style the parser would normalise stays exactly as the customer wrote it."""
    root = _write_repo(
        tmp_path,
        {
            "modules.yaml": "activated: []\n",
            "data-models/odd.yaml": "name:    Odd\ndefinition:\n    attributes: []\n\n\n",
        },
    )
    plan = _engine.plan(root)
    assert [c.path for c in plan.changes] == ["masterly.yaml"]


def test_the_console_script_is_installed_and_says_which_extra_it_needs() -> None:
    """`masterly --version` answers from the installed entry point; the help names the command.
    The import that needs the extra is deferred, so the script starts without it and the error
    is the install line rather than a traceback."""
    answer = subprocess.run(  # noqa: S603 - our own interpreter, literal arguments
        [sys.executable, "-m", "masterly.cli", "--version"], capture_output=True, text=True
    )
    assert answer.returncode == 0 and answer.stdout.startswith("masterly ")
    assert main([]) == 2
    with pytest.raises(SystemExit):
        main(["config", "migrate", "--help"])
