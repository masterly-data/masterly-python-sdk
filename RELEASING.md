# Releasing `masterly`

`masterly` is published to PyPI, where **versions are immutable**: a version number is spent
the moment it is uploaded, and a source that turns out to be wrong is corrected by publishing
a *new* version, never by re-publishing an old one. That is why the release record — the tag
that says which commit an installable artifact was built from — has to be right the first time.

The rule this repository holds itself to:

> **Every version on PyPI is a tag in this repository, and that tag is on `main`.**

Two published versions predate this repository and do not satisfy it. They are recorded as a
known gap below, with the evidence for what they were actually built from, rather than papered
over with a tag pointing at the wrong commit.

## How a release happens

The tag *is* the trigger, so a release cannot happen without one:

1. Land the version bump in `pyproject.toml` on `main` through a pull request. `ci.yml` gates
   it — ruff, mypy, and pytest on both ends of the supported Python range.
2. Tag the merged commit on `main` and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a v0.3.0 -m "v0.3.0 — <what changed>"
   git push origin v0.3.0
   ```

3. The pushed tag starts [`.github/workflows/release.yml`](.github/workflows/release.yml),
   which builds and publishes to PyPI over trusted publishing (OIDC — no API tokens anywhere).

Before it builds anything, that workflow refuses a tag that would produce an unreproducible
release:

| Refusal | Why |
|---|---|
| The tag does not match `pyproject.toml`'s `version` | The artifact would carry a version nobody tagged. PyPI would take it, and the number could never be reused. |
| The tag's commit is not reachable from the default branch | A tag on a side branch is a published artifact whose source is not in the history anyone reads — exactly the defect this document exists for. |
| A version already recorded as a historical gap below | Those numbers are spent on PyPI. Re-using one is not possible, and claiming one is a false provenance record. |

Annotated tags (`git tag -a`) are preferred over lightweight ones: an annotated tag carries a
tagger, a date, and a message, so the record says who cut the release and what it was.

Tags, not GitHub Releases, are the record here. Of the Masterly repositories that publish
something, only `masterly-application-backend` carries GitHub Releases, and only because
release-please writes them; the frontend, the platform backend, the Terraform module and the
demo install all use tags alone. This repository follows that majority.

## Versions published before this repository existed

`masterly-python-sdk` is the **recreated public** repository. Its history begins at
`b3015a5`, an import of the then-current source. The development history through v0.2.0 —
**and the tags for both published versions** — stayed behind in the private, now archived
`masterly-python-sdk-archive`. Nothing carried the tags across, which is why two installable
versions have no release record here.

What each version was actually built from is not in doubt. Every file in each published sdist
was compared byte-for-byte against the archive repository's tag of the same name, and matched:

<!-- release-record:begin -->

| Version | Published (UTC) | Source commit | Where that commit lives | sdist sha256 |
|---|---|---|---|---|
| 0.1.1 | 2026-07-13 | `99baa7f02648e5057b64120d04151db061ea160a` | `masterly-python-sdk-archive`, annotated tag `v0.1.1` | `13fc8a428e412d7289541a007d447c1274f7e01d0df65b55c99e525fda83a09e` |
| 0.2.0 | 2026-09-02 | `88d4051809affff77d1034228ab0ff5ea97a4042` | `masterly-python-sdk-archive`, annotated tag `v0.2.0` | `1ec192d722afd634f1bcca3ef9447014c38d4a70e7dc2e08b34150c51b1714ea` |

<!-- release-record:end -->

**Neither commit is tagged here, and neither one should be.** The commit that produced v0.2.0
is not in this repository's history and is not equal to anything in it: `b3015a5`, the import,
is that commit plus two later documentation edits (a copyright line in `README.md`, an
Environment id in `examples/README.md`). `README.md` ships inside the distribution and becomes
the PyPI project page, so those two edits are a real difference between `b3015a5` and what
customers installed. A tag on `b3015a5` claiming to be v0.2.0 would be a provenance claim a
customer can read and check, and it would be wrong.

Importing the archive commits themselves is not a way out either: their ancestry is the eight
commits of private development history that the archive-and-recreate deliberately did not
publish, and pushing one would publish all of it.

So the gap stands, deliberately, and is written down here instead. The archive repository is
private, so an outside auditor cannot resolve those commit ids themselves; what they *can*
check without our help is the sdist digest, against the one PyPI publishes for the same file.
Ask Masterly if you need the commit itself — the record above is what makes that a question
with a definite answer rather than a shrug.

[`scripts/check_release_record.py`](scripts/check_release_record.py) keeps this section honest.
It runs on every pull request and again on every tag push, and it fails if:

- the table above has stopped parsing — a heading rewrite or a rename that quietly disarmed the
  check reads as a failure, not as a pass;
- a commit listed above has become reachable from this repository's history, which would mean
  the row is stale and the version can and should now be tagged;
- `pyproject.toml`'s version is one of the versions listed above;
- the clone is shallow, where "this commit is not in our history" would be true of everything
  and would prove nothing.

Run it by hand the same way CI does:

```bash
python3 scripts/check_release_record.py --selftest   # the check still catches a bad record
python3 scripts/check_release_record.py              # this repository
```

Standard library only, and no network: it never asks PyPI anything, so it fails on our mistakes
rather than on someone else's outage.
