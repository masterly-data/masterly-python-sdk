# masterly-python-sdk

The public (Apache-2.0) Python client for Masterly, published to PyPI as `masterly`
(`pyproject.toml`). It is a hand-written client over the stable `/v1` REST API, not generated
code and not a second contract: it adds ergonomics to endpoints the server already
publishes (ADR 0068). Sync `httpx`, Python >= 3.10, typed (`src/masterly/py.typed`).

This repository is public. Write everything in it — code, comments, commit messages, PR
text — as something a customer will read.

## Layout

- `src/masterly/` — the package; public names are listed in `__all__` in `__init__.py`
- `src/masterly/cli/` — the `masterly` console script (`masterly config migrate`)
- `tests/` — pytest suite; `examples/demo_data.py` — demo master-data generator
- `scripts/check_release_record.py` — the release-record check (see `RELEASING.md`)

## Commands

Run from the repository root; `uv` manages the environment (`uv.lock` is committed).

```bash
uv sync                                          # install, dev group included
uv run pytest -q                                 # tests
uv run ruff check                                # lint (rules and line length in pyproject.toml)
uv run mypy src                                  # types; strict, on the package only
uv build                                         # sdist and wheel, built with hatchling
python3 scripts/check_release_record.py --selftest && python3 scripts/check_release_record.py
```

`.github/workflows/ci.yml` runs ruff, mypy and pytest on Python 3.10 and 3.13, plus the
release-record check. A change is gated on all of it.

## Versioning and release

- Semver. The version is set in two places that must agree: `pyproject.toml` (`version`)
  and `src/masterly/__init__.py` (`__version__`, sent in the `User-Agent` header).
- The SDK's version tracks the `/v1` contract and is independent of the application's.
- A release is a version-bump PR to `main`, then an annotated `vX.Y.Z` tag on the merged
  commit. Pushing the tag starts `.github/workflows/release.yml`, which publishes to PyPI by
  trusted publishing (OIDC, no tokens) through the `pypi` environment.
- PyPI versions are immutable, and the workflow refuses a tag that does not match
  `pyproject.toml`, is not on the default branch, or claims 0.1.1 or 0.2.0. Never tag,
  publish or re-use a version without being asked to. Details: `RELEASING.md`.

## Documentation

- The SDK reference on masterlydata.com (`Documentation` URL in `pyproject.toml`) documents
  the **released** SDK. Do not describe unreleased behaviour there; `README.md` is what
  PyPI shows, so it must also be true of the released package.
- There is no `CHANGELOG.md` in this repository. The changelog is the docs "What's new"
  page (`Changelog` URL in `pyproject.toml`), which lives outside this repository.

## Don't

- Don't make a breaking change to the public API (anything in `__all__`, method signatures,
  exception types, the `masterly` console script) without a major version.
- Don't add a runtime dependency beyond `httpx` without a stated reason. Optional features
  go in an extra (`pandas`, `cli` in `pyproject.toml`), never in `dependencies`.
- Don't call endpoints outside stable `/v1` paths.
- Don't edit `uv.lock` by hand; change `pyproject.toml` and re-lock.
- Don't weaken a test, a ruff rule or a strict mypy setting to get a change through.
- Don't put credentials, tokens or customer data in code, tests, examples or comments.

## Done means

- `ruff check`, `mypy src` and `pytest -q` pass.
- New behaviour has a test; the existing tests were not weakened.
- `examples/demo_data.py` still runs: `uv run examples/demo_data.py --dry-run --customers 5`
  (`examples/README.md`); `tests/test_demo_data.py` covers the generator.
- A user-visible change is noted for the changelog and `README.md` is updated if it is wrong.
- `REVIEW.md` is the checklist a reviewer applies.
