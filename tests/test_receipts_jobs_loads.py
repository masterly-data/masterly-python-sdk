"""Following ingest to its outcome, over a mock transport: each batch's receipt, reading a job
back, and a full snapshot larger than one call sent as one full load."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import masterly._client as client_module
from masterly import ApiError, Client, IngestReceipt


def _client(handler: Any) -> Client:
    return Client(
        "https://masterly.test",
        token="tok",
        environment="env_prod_eu",
        transport=httpx.MockTransport(handler),
    )


def _records(count: int) -> list[dict[str, Any]]:
    return [{"customer_number": f"C-{i:05d}"} for i in range(count)]


# --- receipts ----------------------------------------------------------------------------


def test_each_batch_keeps_the_receipt_the_platform_answered_in_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": f"job_{len(seen)}"})

    report = _client(handler).sources.ingest("src_1", _records(12_000), batch_size=5000)

    assert report.receipts == (
        IngestReceipt(job_id="job_1", records=5000),
        IngestReceipt(job_id="job_2", records=5000),
        IngestReceipt(job_id="job_3", records=2000),
    )
    assert report.job_ids == ("job_1", "job_2", "job_3")
    # The three fields the report always had keep their meaning.
    assert (report.source_id, report.records, report.batches) == ("src_1", 12_000, 3)
    keys = [request.headers["idempotency-key"] for request in seen]
    assert len(set(keys)) == 3, "each batch is its own write, under its own key"


def test_a_batch_whose_connection_fails_is_resent_under_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether the first attempt landed is unknowable; the key makes the re-send harmless."""
    monkeypatch.setattr(client_module, "_KEYED_RETRY_BACKOFF", (0.0, 0.0))
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        if len(keys) == 1:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(202, json={"job_id": "job_1"})

    report = _client(handler).sources.ingest("src_1", _records(3))

    assert report.job_ids == ("job_1",)
    assert len(keys) == 2 and keys[0] == keys[1]


def test_a_connection_that_never_answers_is_raised_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_KEYED_RETRY_BACKOFF", (0.0, 0.0))
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("unreachable", request=request)

    with pytest.raises(httpx.ConnectError):
        _client(handler).sources.ingest("src_1", _records(3))
    assert len(attempts) == 3


def test_an_answer_is_never_retried() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(503, json={"error": {"code": "UNAVAILABLE", "message": "busy"}})

    with pytest.raises(ApiError, match="UNAVAILABLE"):
        _client(handler).sources.ingest("src_1", _records(3))
    assert len(attempts) == 1


# --- reading a job -----------------------------------------------------------------------


def _job(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": "job_1",
        "run_id": "run_1",
        "kind": "ingest.batch",
        "status": status,
        "attempts": 1,
        "created_at": "2026-10-04T10:00:00Z",
        **extra,
    }


def test_a_job_is_read_back_with_its_status_and_the_error_that_failed_it() -> None:
    error = {"code": "INGEST_SOURCE_MISSING", "message": "The source was deleted", "details": {}}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_job("failed", error=error))

    job = _client(handler).jobs.get("job_1")

    assert paths == ["/v1/jobs/job_1"]
    assert job["status"] == "failed"
    assert job["error"] == error


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_waiting_returns_once_the_job_has_finished(outcome: str) -> None:
    statuses = iter(["queued", "running", outcome])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_job(next(statuses)))

    job = _client(handler).jobs.wait("job_1", timeout=5, interval=0)

    assert job["status"] == outcome


def test_waiting_raises_when_the_job_outlasts_the_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_job("running"))

    with pytest.raises(TimeoutError, match="job job_1 is still 'running'"):
        _client(handler).jobs.wait("job_1", timeout=0.05, interval=0.01)


def test_a_service_account_job_read_is_left_to_the_server_to_refuse() -> None:
    """No client-side persona check: the refusal is the server's, so the call starts working
    the day the platform lets a service account read its own jobs."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            401, json={"error": {"code": "UNAUTHENTICATED", "message": "A session is required"}}
        )

    job = Client.for_service_account(
        "https://masterly.test", token="m2m:dev:svc_example", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ApiError) as refused:
        job.jobs.get("job_1")
    assert refused.value.code == "UNAUTHENTICATED"
    assert [request.url.path for request in seen] == ["/v1/jobs/job_1"]


# --- full loads --------------------------------------------------------------------------


def _load_view(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "load_id": "load_1",
        "source_id": "src_1",
        "status": status,
        "batches": 3,
        "records": 12_000,
        "reconciled": None,
        "job_id": None,
        "opened_at": "2026-10-04T10:00:00Z",
        "updated_at": "2026-10-04T10:01:00Z",
        "completed_at": None,
        **extra,
    }


class _LoadServer:
    """Answers the full-load routes, recording every request. ``fail_batch`` makes that
    (1-based) batch of the load answer 400."""

    def __init__(self, fail_batch: int | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_batch = fail_batch
        self.batches = 0

    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/sources/src_1/loads":
            return httpx.Response(201, json=_load_view("open", batches=0, records=0))
        if path == "/v1/ingest":
            self.batches += 1
            if self.batches == self.fail_batch:
                return httpx.Response(
                    400, json={"error": {"code": "VALIDATION_ERROR", "message": "bad batch"}}
                )
            return httpx.Response(202, json={"job_id": f"job_{self.batches}"})
        if path == "/v1/sources/src_1/loads/load_1:complete":
            return httpx.Response(202, json={"job_id": "job_reconcile"})
        if path == "/v1/sources/src_1/loads/load_1:abandon":
            return httpx.Response(200, json=_load_view("abandoned"))
        if request.method == "GET" and path == "/v1/sources/src_1/loads/load_1":
            return httpx.Response(200, json=_load_view("completing", job_id="job_reconcile"))
        raise AssertionError(f"unexpected request {request.method} {path}")


def test_a_snapshot_larger_than_one_call_goes_as_one_load_and_completes() -> None:
    server = _LoadServer()

    with _client(server).sources.full_load("src_1", batch_size=5000) as load:
        load.send(_records(12_000))

    assert server.calls() == [
        "POST /v1/sources/src_1/loads",
        "POST /v1/ingest",
        "POST /v1/ingest",
        "POST /v1/ingest",
        "POST /v1/sources/src_1/loads/load_1:complete",
        "GET /v1/sources/src_1/loads/load_1",
    ]
    batches = [json.loads(r.content) for r in server.requests if r.url.path == "/v1/ingest"]
    assert [len(b["records"]) for b in batches] == [5000, 5000, 2000]
    assert all(b["mode"] == "full" and b["load_id"] == "load_1" for b in batches)
    assert all(b["source_id"] == "src_1" for b in batches)
    # Every write the platform de-duplicates carries its own key: the open and each batch.
    keyed = server.requests[:4]
    keys = [r.headers.get("idempotency-key") for r in keyed]
    assert all(keys) and len(set(keys)) == 4

    report = load.report
    assert load.load_id == "load_1"
    assert (report.source_id, report.records, report.batches) == ("src_1", 12_000, 3)
    assert report.job_ids == ("job_1", "job_2", "job_3")
    assert report.load is not None and report.load["status"] == "completing"
    assert report.load["job_id"] == "job_reconcile"


def test_a_load_may_be_sent_a_piece_at_a_time() -> None:
    server = _LoadServer()

    with _client(server).sources.full_load("src_1", batch_size=5000) as load:
        first = load.send(_records(3000))
        load.send(_records(3000))

    assert [r.job_id for r in first] == ["job_1"]
    assert load.report.records == 6000
    assert load.report.job_ids == ("job_1", "job_2")


def test_a_failed_batch_abandons_the_load_and_never_completes_it() -> None:
    server = _LoadServer(fail_batch=2)

    with (
        pytest.raises(ApiError, match="VALIDATION_ERROR"),
        _client(server).sources.full_load("src_1", batch_size=5000) as load,
    ):
        load.send(_records(12_000))

    assert server.calls() == [
        "POST /v1/sources/src_1/loads",
        "POST /v1/ingest",
        "POST /v1/ingest",
        "POST /v1/sources/src_1/loads/load_1:abandon",
    ]
    with pytest.raises(RuntimeError, match="not been completed"):
        _ = load.report


def test_an_error_in_the_callers_own_code_abandons_the_load_too() -> None:
    server = _LoadServer()

    with (
        pytest.raises(KeyError),
        _client(server).sources.full_load("src_1", batch_size=5000) as load,
    ):
        load.send(_records(10))
        raise KeyError("the next frame could not be read")

    assert server.calls()[-1] == "POST /v1/sources/src_1/loads/load_1:abandon"
    assert not any(call.endswith(":complete") for call in server.calls())


def test_a_load_that_sent_nothing_is_abandoned_rather_than_completed() -> None:
    server = _LoadServer()

    with (
        pytest.raises(ValueError, match="at least one record"),
        _client(server).sources.full_load("src_1"),
    ):
        pass

    assert server.calls() == [
        "POST /v1/sources/src_1/loads",
        "POST /v1/sources/src_1/loads/load_1:abandon",
    ]


def test_a_load_already_in_flight_is_refused_and_nothing_else_is_sent() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "INGEST_LOAD_IN_FLIGHT",
                    "message": "A load of this source is open",
                    "details": {"load_id": "load_0"},
                }
            },
        )

    with (
        pytest.raises(ApiError) as refused,
        _client(handler).sources.full_load("src_1") as load,
    ):
        load.send(_records(1))  # pragma: no cover - never reached

    assert refused.value.details == {"load_id": "load_0"}
    assert len(sent) == 1


def test_waiting_for_a_load_returns_how_many_records_it_deleted() -> None:
    views = iter(
        [
            _load_view("completing", job_id="job_reconcile"),
            _load_view("completed", job_id="job_reconcile", reconciled=42),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sources/src_1/loads/load_1"
        return httpx.Response(200, json=next(views))

    done = _client(handler).sources.wait_for_load("src_1", "load_1", timeout=5, interval=0)

    assert done["status"] == "completed"
    assert done["reconciled"] == 42


def test_a_full_snapshot_that_fits_one_call_still_goes_as_one_call_and_no_load() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"job_id": "job_1"})

    records = _records(5000)
    report = _client(handler).sources.ingest("src_1", records, mode="full", batch_size=5000)

    assert [f"{r.method} {r.url.path}" for r in seen] == ["POST /v1/ingest"]
    assert json.loads(seen[0].content) == {"source_id": "src_1", "records": records, "mode": "full"}
    assert report.load is None
    assert report.job_ids == ("job_1",)
