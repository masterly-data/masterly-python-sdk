# Security policy

Thank you for taking the time to report a problem. This client is published to PyPI as
[`masterly`](https://pypi.org/project/masterly/) and holds API tokens on behalf of whoever runs
it, so we would much rather hear from you privately first.

## Reporting a vulnerability

**Please do not open a public issue.** A public report is visible to everyone running an affected
version before any of them can act on it.

Use either of these instead:

1. **GitHub private vulnerability reporting** (preferred) — the **Report a vulnerability** button
   on this repository's [Security tab](https://github.com/masterly-data/masterly-python-sdk/security).
   It opens a private draft advisory only you and the maintainers can see, and it keeps the
   discussion attached to the code.
2. **Email** — [christer.larsson@masterlydata.com](mailto:christer.larsson@masterlydata.com),
   with `SECURITY` in the subject line.

Please do not use `support@` or `privacy@` for vulnerability reports. They are real addresses
handled on a normal support rhythm, which is the wrong one for this.

## What to include

Whatever you have. A partial report is worth sending — we would rather triage something thin than
never hear about it. If you can, the most useful things are:

- The `masterly` version and the Python version.
- What an attacker gains, and who has to be who for it to work.
- A minimal snippet that reproduces it, with any real tokens removed.

## What to expect

Masterly is a small team, so these are windows we can actually hold rather than aspirational ones:

| | |
|---|---|
| Acknowledgement | Within **5 business days** |
| Initial assessment | Within **10 business days** — severity, whether we can reproduce it, and a rough plan |
| Fix and disclosure | By agreement with you. We will tell you when a fix ships and credit you unless you would rather we did not |

If you have not heard back inside the acknowledgement window, please assume the message went
astray and send it again — persistence is welcome, not a nuisance.

## Scope

**In scope** — this repository and the `masterly` package on PyPI: the client, its examples, and
its CI and release workflows.

Especially interesting: anything that leaks or mishandles a caller's token, anything that sends a
request somewhere the caller did not ask for, and anything in the publishing path that could put
a package on PyPI that this repository did not build.

**Out of scope** — a Masterly install the SDK talks to. A self-hosted install runs in its
operator's own subscription; if you believe the flaw is in the service rather than the client,
say so in the report and we will route it.

## Supported versions

Fixes land on the latest released version on PyPI. Please upgrade to the patched release rather
than expecting a backport to an older one.
