"""The release record — a tag per published version — is what this script forces to stay true.

`masterly` is installable from PyPI, where versions are immutable. A published artifact whose
source commit nobody wrote down is a release that cannot be reproduced, diffed, or rolled back
to, and the remedy is never a re-publish: it is a new version number and an explanation. Two
versions, 0.1.1 and 0.2.0, are already in that state — they were published from the private
repository this public one replaced, and the archive-and-recreate did not carry their tags
across (MAS-503). `RELEASING.md` records what they were actually built from.

A record nobody is forced to update is a documented good intention, so this is the forcing
function, and CI runs it three times over:

  * on every pull request and push to main — the gap table must still parse, must still be
    true, and `pyproject.toml` must not be about to ship a version already spent on PyPI;
  * on a tag push (`--tag vX.Y.Z`) — the same, plus the tag must match the package version,
    so a bad tag turns the release build red before anything reaches PyPI;
  * before either, as `--selftest` — synthetic trees prove the check still rejects a missing,
    malformed or stale record, so a detector that has quietly stopped detecting fails loudly
    instead of passing this repository for the wrong reason.

It transcribes nothing. Every version, commit id and digest it reasons about is read out of
`RELEASING.md` or `pyproject.toml` at the moment it runs; a checker carrying its own copy of
those values would be the very defect it exists to catch. Where it derives a set of things to
check, it asserts the set is non-empty first — a heading rewrite or a file rename that left the
table unparseable would otherwise disarm the check into a silent pass.

Standard library only, and no network. It never asks PyPI anything: a gate that fails on
someone else's outage is one people learn to ignore.

Run:
    python3 scripts/check_release_record.py
    python3 scripts/check_release_record.py --tag v0.3.0
    python3 scripts/check_release_record.py --selftest
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RELEASING_PATH = Path(os.environ.get("RELEASE_RECORD_PATH", REPO_ROOT / "RELEASING.md"))
PYPROJECT_PATH = Path(os.environ.get("RELEASE_PYPROJECT_PATH", REPO_ROOT / "pyproject.toml"))
GIT_ROOT = Path(os.environ.get("RELEASE_GIT_ROOT", REPO_ROOT))

# The fenced region of RELEASING.md that holds the versions published before this repository
# existed. Comment markers rather than a heading: a heading is prose and gets reworded, and a
# reworded heading that silently matches nothing is how this check would stop checking.
BEGIN_MARKER = "<!-- release-record:begin -->"
END_MARKER = "<!-- release-record:end -->"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
# `version = "0.3.0"` under [project]. Anchored to the line start so a dependency's version
# constraint elsewhere in the file cannot be mistaken for the package's own.
PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


class CheckFailed(Exception):
    """A finding worth failing the build for. The message is the whole report."""


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def parse_package_version(pyproject_text: str) -> str:
    matches = PYPROJECT_VERSION_RE.findall(pyproject_text)
    if not matches:
        raise CheckFailed(
            "no `version = \"...\"` line found in pyproject.toml. Either the package version "
            "moved (to a dynamic version, or under a different key) or this check is reading "
            "the wrong file — both leave the release record unguarded, so this is a failure "
            "rather than a skip."
        )
    version = matches[0]
    if not VERSION_RE.match(version):
        raise CheckFailed(
            f"pyproject.toml's version {version!r} is not a plain X.Y.Z. The release workflow "
            "compares it to a `vX.Y.Z` tag, so a version it cannot compare is a release "
            "nobody can tag."
        )
    return version


def parse_gap_table(releasing_text: str) -> list[dict[str, str]]:
    """Read the published-before-this-repository rows out of RELEASING.md.

    Fails rather than returns nothing: an empty result here is indistinguishable from "the
    record is clean", and the two must not look alike.
    """
    if BEGIN_MARKER not in releasing_text or END_MARKER not in releasing_text:
        raise CheckFailed(
            f"{RELEASING_PATH.name} no longer contains the {BEGIN_MARKER} / {END_MARKER} "
            "markers around the table of versions published before this repository existed. "
            "Without them this check has nothing to verify and would pass for the wrong "
            "reason. Restore the markers, or — if the gap has genuinely been closed — remove "
            "this check in the same change that removes the section."
        )
    body = releasing_text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0]

    rows: list[dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        version, published, commit, lives, digest = cells[0], cells[1], cells[2], cells[3], cells[4]
        if not VERSION_RE.match(version):
            continue  # the header row and its `|---|` separator
        rows.append(
            {
                "version": version,
                "published": published,
                "commit": commit,
                "lives": lives,
                "digest": digest,
            }
        )

    if not rows:
        raise CheckFailed(
            f"the release-record table in {RELEASING_PATH.name} parsed to zero rows. The "
            "markers are present, so the table's shape has changed under this check — five "
            "columns are expected (version, published, source commit, where it lives, sdist "
            "sha256). A check that silently matches nothing is worse than no check."
        )

    for row in rows:
        if not COMMIT_RE.match(row["commit"]):
            raise CheckFailed(
                f"version {row['version']} names source commit {row['commit']!r}, which is not "
                "a full 40-character commit id. An abbreviated or approximate commit is not a "
                "provenance record."
            )
        if not SHA256_RE.match(row["digest"]):
            raise CheckFailed(
                f"version {row['version']} names sdist digest {row['digest']!r}, which is not a "
                "64-character sha256. The digest is the only part of this row a reader can "
                "verify against PyPI without our help."
            )
        if not row["lives"]:
            raise CheckFailed(
                f"version {row['version']} does not say which repository holds its source "
                "commit. A commit id with no repository is not findable."
            )

    return rows


# --------------------------------------------------------------------------------------
# Git probes
# --------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def assert_history_is_readable(root: Path) -> None:
    """The absence tests below are only meaningful in a full clone.

    In a shallow clone almost every commit is "not in our history", so every row would pass and
    the check would be a rubber stamp. Two assertions guard that: the clone is not shallow, and
    the ancestry probe demonstrably still answers yes for a commit that *is* in the history.
    """
    shallow = git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        raise CheckFailed(
            f"git could not be run in {root}: {shallow.stderr.strip() or 'unknown error'}. "
            "This check reasons about history and cannot run without it."
        )
    if shallow.stdout.strip() == "true":
        raise CheckFailed(
            "this is a shallow clone. Every commit looks absent from a shallow history, so the "
            "release-record check would pass without checking anything. Fetch the full history "
            "(`fetch-depth: 0` in CI) before running it."
        )

    head = git(root, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise CheckFailed(
            "HEAD does not resolve to a commit; there is no history to check against."
        )

    # Positive control: the probe used below must say yes to something it should say yes to.
    # If it does not, the probe is broken and its "no" answers mean nothing.
    control = git(root, "merge-base", "--is-ancestor", head.stdout.strip(), "HEAD")
    if control.returncode != 0:
        raise CheckFailed(
            "the ancestry probe (`git merge-base --is-ancestor`) failed on HEAD against itself. "
            "It is the only thing standing between a stale release record and a green build, so "
            "a probe that cannot answer a question it must answer is a failure."
        )


def commit_is_in_history(root: Path, commit: str) -> bool:
    """Is `commit` reachable from HEAD in this repository?

    A non-zero exit covers both "known object, not an ancestor" and "unknown object". Both mean
    the same thing for our purposes — the commit is not part of the history this repository
    publishes — and `assert_history_is_readable` has already ruled out the one case where that
    conclusion would be unsound.
    """
    return git(root, "merge-base", "--is-ancestor", commit, "HEAD").returncode == 0


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def check(
    releasing_text: str,
    pyproject_text: str,
    git_root: Path,
    tag: str | None = None,
) -> list[str]:
    """Return the human-readable lines describing what was verified. Raises on a finding."""
    rows = parse_gap_table(releasing_text)
    package_version = parse_package_version(pyproject_text)
    assert_history_is_readable(git_root)

    gap_versions = {row["version"] for row in rows}

    for row in rows:
        if commit_is_in_history(git_root, row["commit"]):
            raise CheckFailed(
                f"{RELEASING_PATH.name} says version {row['version']} was built from commit "
                f"{row['commit']}, which it describes as living in {row['lives']} rather than "
                "here — but that commit is now reachable from this repository's history.\n\n"
                "The record is stale, and in the good direction: the release can now have a "
                "real tag.\n\n"
                f"    git tag -a v{row['version']} {row['commit'][:12]} "
                f"-m 'v{row['version']}'\n"
                f"    git push origin v{row['version']}\n\n"
                f"Then delete the {row['version']} row from the table."
            )

    if package_version in gap_versions:
        raise CheckFailed(
            f"pyproject.toml is set to version {package_version}, which {RELEASING_PATH.name} "
            "records as already published to PyPI. PyPI versions are immutable — an upload "
            "under this number will be rejected, and the number cannot be reclaimed. Choose "
            "the next unused version."
        )

    verified = [
        f"release record: {len(rows)} version(s) published before this repository "
        f"({', '.join(sorted(gap_versions))}), each with a source commit and sdist digest, "
        "and none of those commits is in this repository's history",
        f"pyproject.toml version {package_version} is not a version already spent on PyPI",
    ]

    if tag is not None:
        match = TAG_RE.match(tag)
        if not match:
            raise CheckFailed(
                f"tag {tag!r} is not of the form vX.Y.Z. The release workflow only fires on "
                "`v*`, and a tag it cannot parse is a release nobody can match to a version."
            )
        tag_version = match.group(1)
        if tag_version in gap_versions:
            raise CheckFailed(
                f"tag {tag} claims version {tag_version}, which {RELEASING_PATH.name} records "
                "as published from a commit that is not in this repository. Tagging a commit "
                "here under that number would be a provenance claim a customer can read and "
                "check, and it would be wrong. If the history has since been imported, the "
                "table row is what should change first."
            )
        if tag_version != package_version:
            raise CheckFailed(
                f"tag {tag} does not match pyproject.toml's version {package_version}. The "
                "artifact would be published under a number nobody tagged."
            )
        verified.append(f"tag {tag} matches the package version and claims no spent version")

    return verified


# --------------------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------------------

_GOOD_TABLE = f"""
{BEGIN_MARKER}

| Version | Published (UTC) | Source commit | Where that commit lives | sdist sha256 |
|---|---|---|---|---|
| 0.1.1 | 2026-07-13 | `{'a' * 40}` | `somewhere-else`, tag `v0.1.1` | `{'b' * 64}` |

{END_MARKER}
"""

_GOOD_PYPROJECT = '[project]\nname = "masterly"\nversion = "0.3.0"\n'


def _fixture_repo(tmp: Path) -> Path:
    """A real, non-shallow git repository with two commits — enough for the ancestry probe."""
    root = tmp / "repo"
    root.mkdir()
    env_args = [
        "-c",
        "user.email=selftest@example.invalid",
        "-c",
        "user.name=selftest",
    ]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for n in ("one", "two"):
        (root / f"{n}.txt").write_text(n)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), *env_args, "commit", "-q", "-m", n], check=True
        )
    return root


def _expect_failure(label: str, **kwargs: object) -> None:
    try:
        check(**kwargs)  # type: ignore[arg-type]
    except CheckFailed:
        print(f"  ok    {label} is rejected")
        return
    raise SystemExit(f"  FAIL  {label} was ACCEPTED — this check no longer detects it")


def selftest() -> int:
    print("selftest: the release-record check still rejects a bad record")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        root = _fixture_repo(tmp)
        head = git(root, "rev-parse", "HEAD").stdout.strip()

        # Positive control: a well-formed record over a real repository passes. Without this,
        # every rejection below could be the check failing for an unrelated reason.
        check(_GOOD_TABLE, _GOOD_PYPROJECT, root)
        print("  ok    a well-formed record is accepted")
        check(_GOOD_TABLE, _GOOD_PYPROJECT, root, tag="v0.3.0")
        print("  ok    a matching tag is accepted")

        _expect_failure(
            "a record whose markers are gone",
            releasing_text="| 0.1.1 | 2026-07-13 | x | y | z |",
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
        )
        _expect_failure(
            "a record whose table has no rows",
            releasing_text=f"{BEGIN_MARKER}\n\nnothing here\n\n{END_MARKER}",
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
        )
        _expect_failure(
            "an abbreviated source commit",
            releasing_text=_GOOD_TABLE.replace("a" * 40, "a" * 7),
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
        )
        _expect_failure(
            "a digest that is not a sha256",
            releasing_text=_GOOD_TABLE.replace("b" * 64, "not-a-digest"),
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
        )
        _expect_failure(
            "a source commit that is actually in this history (a stale row)",
            releasing_text=_GOOD_TABLE.replace("a" * 40, head),
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
        )
        _expect_failure(
            "a package version already spent on PyPI",
            releasing_text=_GOOD_TABLE,
            pyproject_text=_GOOD_PYPROJECT.replace("0.3.0", "0.1.1"),
            git_root=root,
        )
        _expect_failure(
            "a pyproject with no version at all",
            releasing_text=_GOOD_TABLE,
            pyproject_text='[project]\nname = "masterly"\n',
            git_root=root,
        )
        _expect_failure(
            "a tag that does not match the package version",
            releasing_text=_GOOD_TABLE,
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
            tag="v9.9.9",
        )
        _expect_failure(
            "a tag claiming a version published from elsewhere",
            releasing_text=_GOOD_TABLE,
            pyproject_text=_GOOD_PYPROJECT.replace("0.3.0", "0.1.1"),
            git_root=root,
            tag="v0.1.1",
        )
        _expect_failure(
            "a tag that is not vX.Y.Z",
            releasing_text=_GOOD_TABLE,
            pyproject_text=_GOOD_PYPROJECT,
            git_root=root,
            tag="release-0.3.0",
        )

        # A shallow clone must fail rather than pass vacuously: it is the one state in which
        # "that commit is not in our history" is true of everything.
        shallow = tmp / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{root}", str(shallow)], check=True
        )
        _expect_failure(
            "a shallow clone, where absence proves nothing",
            releasing_text=_GOOD_TABLE,
            pyproject_text=_GOOD_PYPROJECT,
            git_root=shallow,
        )

    print("selftest: passed")
    return 0


# --------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="the tag being released (vX.Y.Z); also checks it against the package version",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check the checker against synthetic records instead of this repository",
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    try:
        releasing_text = RELEASING_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL  cannot read {RELEASING_PATH}: {exc}", file=sys.stderr)
        return 1
    try:
        pyproject_text = PYPROJECT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL  cannot read {PYPROJECT_PATH}: {exc}", file=sys.stderr)
        return 1

    try:
        verified = check(releasing_text, pyproject_text, GIT_ROOT, tag=args.tag)
    except CheckFailed as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    for line in verified:
        print(f"ok    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
