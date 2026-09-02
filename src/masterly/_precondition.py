"""The precondition a governed write states, and the refusal it produces (ADR 0070).

A governed write — one where more than one principal may write the object, and where the
request's content was derived from an earlier read of it — says which revision it replaces. The
server refuses when that revision has moved, **and names what moved without saying what it moved
to**. This module is the client half: the value that goes out, and the typed view of what comes
back.

Two spellings of one value, and the gap between them is why this is a type rather than a string.
A single-object read publishes its revision twice — an ``ETag`` header (a quoted entity-tag,
``"7"``) and a ``version`` field in the read model (the bare token, and an *integer* in most
read models today). ``If-Match`` accepts only the entity-tag form under strong comparison: the
server refuses an unquoted tag, a weak validator and a tag list with 400 PRECONDITION_MALFORMED
rather than guessing. So the obvious move — putting ``obj["version"]`` straight into the header —
is refused on the wire, and the failure arrives one round trip later than the mistake. The token
itself stays opaque: this module quotes and unquotes the HTTP framing around it and never reads,
compares or orders the token inside.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: A stale precondition: the revision named in ``If-Match`` is no longer the object's revision.
VERSION_CONFLICT = "VERSION_CONFLICT"
#: The object was deleted or archived under the editor — structural, so nothing to merge into.
OBJECT_REMOVED = "OBJECT_REMOVED"
#: The operation no longer accepts a write without a precondition.
PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
#: The precondition was sent but could not be parsed under strong comparison.
PRECONDITION_MALFORMED = "PRECONDITION_MALFORMED"

_UNCONDITIONAL = "*"


@dataclass(frozen=True)
class Precondition:
    """The revision a governed write replaces, ready for the wire.

    Build it from the ``version`` field of the object you read, then send it with the write::

        policy = client.request("GET", "/v1/access-policies/pol_1")
        client.request(
            "PUT",
            "/v1/access-policies/pol_1",
            json=edited,
            if_match=Precondition.from_version(policy["version"]),
        )
    """

    token: str | None
    """The opaque revision, or None for the unconditional precondition."""

    @classmethod
    def from_version(cls, version: str | int) -> Precondition:
        """From the ``version`` field of a read model — the bare token, usually an integer.

        Refuses what cannot be a token: an entity-tag (use :meth:`from_etag`), a weak validator,
        and a tag list. Refusing here rather than on the wire keeps the mistake next to the line
        that made it, and costs no request.
        """
        if isinstance(version, str):
            value = version.strip()
        else:
            value = str(version)
        if not value:
            raise ValueError("a precondition needs a version — the `version` field of your read")
        if value == _UNCONDITIONAL:
            return cls(token=None)
        if value.startswith("W/"):
            raise ValueError(
                'a weak validator (W/"…") never matches under strong comparison; send the '
                "`version` from your last read"
            )
        if '"' in value:
            raise ValueError(
                "that is an entity-tag, not a version — use Precondition.from_etag() for an "
                "ETag header, or pass the read model's `version` field"
            )
        if "," in value:
            raise ValueError(
                "a governed write states the one revision it replaces, so a tag list is refused"
            )
        return cls(token=value)

    @classmethod
    def from_etag(cls, etag: str) -> Precondition:
        """From the ``ETag`` header of a single-object read. Equal by construction to
        :meth:`from_version` of the same read's ``version`` field — one value in two places."""
        value = etag.strip()
        if value == _UNCONDITIONAL:
            return cls(token=None)
        if value.startswith("W/"):
            raise ValueError('a weak validator (W/"…") never matches under strong comparison')
        if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
            raise ValueError(f"not a strong entity-tag: {etag!r}")
        return cls(token=value[1:-1])

    @classmethod
    def unconditional(cls) -> Precondition:
        """``If-Match: *`` — "it must exist; I do not care which revision".

        A deliberate, legible overwrite for an operator script. The server records it in the
        audit trail as an unconditional write, which is the whole point: a spelled bypass that
        leaves a trace beats an omitted header that leaves none.
        """
        return cls(token=None)

    @property
    def header_value(self) -> str:
        """The ``If-Match`` value: the token as a strong entity-tag, or ``*``."""
        if self.token is None:
            return _UNCONDITIONAL
        return f'"{self.token}"'


def coerce(precondition: Precondition | str | int) -> Precondition:
    """A precondition from what a caller had to hand. A bare string or integer is read as a
    ``version``, since that is what a read model hands you."""
    if isinstance(precondition, Precondition):
        return precondition
    return Precondition.from_version(precondition)


@dataclass(frozen=True)
class Conflict:
    """Why a governed write was refused — what moved, and never what it moved to.

    There is no "theirs" column here and there will not be one: the server does not send the
    other party's values, because a 409 that helpfully returned them would be a read the caller
    may not be entitled to, delivered by the error path. To show a three-way compare, re-read the
    object; that runs the ordinary authorization and access-policy path, and where it is refused
    there is genuinely no "take theirs" to offer.
    """

    code: str
    """VERSION_CONFLICT or OBJECT_REMOVED."""
    object_type: str | None
    object_id: str | None
    base_version: str | None
    """The revision your write stated it was replacing. None if you wrote unconditionally."""
    current_version: str | None
    """The revision the object is at now. None when the object is gone."""
    changed_by: str | None
    changed_at: datetime | None
    changed_fields: tuple[str, ...]
    """The NAMES of the fields that moved, filtered to those you may read. Never values."""
    undisclosed_changes: int
    """How many moved fields you may not be told the names of. Say it out loud — "and N further
    changes you cannot see" — but do not gate on it; see :attr:`may_auto_merge`."""
    changed_fields_complete: bool
    """True only when the change set is fully known AND fully disclosed to you."""
    request_id: str | None

    @property
    def removed(self) -> bool:
        """The object is gone. Structural rather than mergeable: there is nothing to merge into,
        and the write cannot be retried against a newer revision of it."""
        return self.code == OBJECT_REMOVED

    @property
    def may_auto_merge(self) -> bool:
        """The gate on merging your edit with the other party's, and it is
        ``changed_fields_complete`` — **never** ``undisclosed_changes == 0``.

        A zero count means nothing was withheld from *you*. It is also what you get when the
        writer recorded no field list at all, which is *unknown*, not *empty*; a client gating on
        the count would auto-merge over changes nobody enumerated, which is the silent overwrite
        re-entering through the refusal that exists to stop it.

        True is necessary, not sufficient: it says the change set can be trusted as the complete
        list of what moved. Whether your own edits are disjoint from it is still yours to decide,
        and a merge that is not disjoint is a decision for a person.
        """
        return self.changed_fields_complete and not self.removed

    @classmethod
    def from_details(cls, code: str, details: Mapping[str, Any]) -> Conflict:
        """Read the refusal's ``details``. Tolerant by intent: a field the server has not sent
        yet must not turn a conflict a caller can act on into a parse error they cannot."""
        fields = details.get("changed_fields")
        return cls(
            code=code,
            object_type=_text(details.get("object_type")),
            object_id=_text(details.get("object_id")),
            base_version=_text(details.get("base_version")),
            current_version=_text(details.get("current_version")),
            changed_by=_text(details.get("changed_by")),
            changed_at=_timestamp(details.get("changed_at")),
            changed_fields=tuple(str(f) for f in fields) if isinstance(fields, list) else (),
            undisclosed_changes=_count(details.get("undisclosed_changes")),
            # Absent means not complete. The safe reading of a missing gate is the closed one.
            changed_fields_complete=details.get("changed_fields_complete") is True,
            request_id=_text(details.get("request_id")),
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Python 3.10's fromisoformat does not take the military 'Z' the API emits.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
