# Reviewing a change to masterly-python-sdk

`CLAUDE.md` says how to work in this repository; this file says what a review blocks on.

## What blocks

- **The card's criteria are not met.** Check the change against the stated acceptance
  criteria, not against what the diff happens to do.
- **A public API break without a major version.** Removed or renamed names in `__all__`
  (`src/masterly/__init__.py`), changed signatures or defaults, a new required argument, a
  changed exception type, or a changed `masterly` console script. PyPI versions are
  immutable, so a break cannot be taken back.
- **A new runtime dependency.** Any change to `dependencies` in `pyproject.toml` beyond
  `httpx`. Optional features belong in an extra.
- **A missing changelog entry** for a user-visible change. The changelog is the docs
  "What's new" page (`Changelog` URL in `pyproject.toml`), not a file here, so look for the
  entry in the PR description or the linked docs change.
- **A weakened check:** a skipped or loosened test, a new ruff ignore, a relaxed strict
  mypy setting.
- **A release that breaks the record:** a version bump that differs between
  `pyproject.toml` and `src/masterly/__init__.py`, or one that reuses 0.1.1 or 0.2.0
  (`RELEASING.md`, `scripts/check_release_record.py`).
- **Secrets or customer data** anywhere in the diff. The repository is public.

## What does not block

- Style, formatting and import order that ruff owns (`uv run ruff check`). If the linter
  accepts it, do not comment on it.
- Typing details mypy accepts under its strict configuration.
- Dependabot bumps of dev tools and actions that CI passes (`.github/dependabot.yml`).
- Wording preferences in comments and docs that are accurate.

## Known traps

- **The version lives in two places**, `pyproject.toml` and `src/masterly/__init__.py`.
  Updating one passes CI's checks but ships a wrong `User-Agent` and `--version`.
- **`README.md` ships to PyPI** as the project page, so a README edit is a published change
  once released, and must describe the released package only.
- **Two published versions have no tag here** (0.1.1, 0.2.0). Do not "fix" that by tagging
  an unrelated commit; `RELEASING.md` explains why.
- **mypy runs on `src` only.** Tests and `examples/` are not type-checked, so a typing
  problem there will not fail CI.
- **`examples/` is not covered by `pytest`** except through `tests/test_demo_data.py`;
  run the example with `--dry-run` when a change touches the client surface it uses.
- **Governed writes need a revision.** `data_models.update` requires the precondition
  version; do not make it optional to simplify a call site.
- **`uv.lock` is regenerated, not edited.** A hand-edited lockfile is a review finding.
