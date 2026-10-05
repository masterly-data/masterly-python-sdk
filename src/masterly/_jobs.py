"""Jobs: follow asynchronous work to its outcome.

Heavy work is accepted with ``202`` and a job receipt (``{"job_id": ...}``) and runs in the
background — an ingest batch, a full load's reconciliation, a restore. ``client.jobs`` reads
one job back by that id, and waits for it to finish when you need its outcome before moving on.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from masterly._client import Client

#: The statuses a job does not leave on its own.
JOB_TERMINAL = frozenset({"succeeded", "failed"})

_TIMEOUT = 300.0
_INTERVAL = 2.0


def poll(
    read: Callable[[], dict[str, Any]],
    *,
    terminal: Collection[str],
    timeout: float,
    interval: float,
    what: str,
) -> dict[str, Any]:
    """Read until ``status`` is one of ``terminal``, and return that read.

    Raises :class:`TimeoutError` once ``timeout`` seconds have passed without one. The last
    status seen is in the message, so a caller can tell a queue that never started from work
    that is still running.
    """
    if timeout < 0 or interval < 0:
        raise ValueError("timeout and interval are seconds, and cannot be negative")
    deadline = time.monotonic() + timeout
    while True:
        view = read()
        status = view.get("status")
        if status in terminal:
            return view
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{what} is still '{status}' after {timeout:g}s")
        time.sleep(min(interval, remaining))


class JobsApi:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self, job_id: str) -> dict[str, Any]:
        """One job: its ``status`` (``queued``, ``running``, ``succeeded`` or ``failed``),
        ``attempts``, timestamps, and — for a failed job — the ``error`` that ended it
        (``code``, ``message``, ``details``). A job waiting out a retry after a transient fault
        stays ``queued`` and carries ``next_attempt_at``.

        Reading a job is a session route today: it needs a session token whose role may read
        data. On a service-account connection the server refuses it, and the refusal is raised
        as :class:`~masterly.ApiError` as the server sent it. This client does not check the
        persona first, so the call starts working on a service account the day the platform
        lets that persona read its own jobs, with no client upgrade.
        """
        job: dict[str, Any] = self._client._request("GET", f"/v1/jobs/{job_id}")
        return job

    def wait(
        self, job_id: str, *, timeout: float = _TIMEOUT, interval: float = _INTERVAL
    ) -> dict[str, Any]:
        """Read the job every ``interval`` seconds until it has ``succeeded`` or ``failed``,
        and return that read. A failed job is returned, not raised: its ``error`` says why,
        and whether that is an exception is yours to decide.

        Raises :class:`TimeoutError` when the job has not finished within ``timeout`` seconds.
        The job itself carries on — waiting is only a read. Same persona rule as :meth:`get`.
        """
        return poll(
            lambda: self.get(job_id),
            terminal=JOB_TERMINAL,
            timeout=timeout,
            interval=interval,
            what=f"job {job_id}",
        )
