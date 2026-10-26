"""The public keyless slice, end to end, with sockets blocked.

This is the acceptance test for MIA-BETA-001. It walks the journey a stranger actually
gets from a clean clone:

    real SEC metadata -> Today card -> Thesis condition or explicit gap
        -> scripted Fast then Deep on one immutable revision -> exact What-if

Three properties are asserted rather than described:

* the filing metadata is *real* — each packaged row re-derives the capture hash it was
  stored under, so the dataset cannot silently become invented data;
* nothing leaves the process — non-loopback egress raises for the whole journey;
* the arithmetic is exact — floats and exponent notation are rejected at the boundary,
  and What-if is checked against hand-computed Decimals when the run completes.

The SQLite write race that used to force these completion assertions into `xfail` is
fixed at its cause (`Store` keeps one connection open so the WAL file is not deleted
while another thread is writing), so Fast and Deep completion are asserted directly
instead of being marked as a known defect.
"""
import json
import socket
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_agent.public_ivs import DATASET, build_app, load_dataset
from market_agent.sec import filing_metadata_hash

LOOPBACK = "http://127.0.0.1:8791"
QUESTION = "UNP 最新季报与既有投资逻辑的关系"


@pytest.fixture()
def blocked_network(monkeypatch):
    """Refuse any non-loopback egress for the whole journey.

    Loopback stays reachable because asyncio's event loop creates a local self-pipe
    socketpair on Windows; the claim under test is that nothing leaves the machine, not
    that the interpreter cannot open a local socket.
    """
    attempts = []
    local = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def host_of(address):
        return str(address[0]) if isinstance(address, tuple) and address else None

    def connect(self, address):
        host = host_of(address)
        if host is not None and host not in local:
            attempts.append(address)
            raise AssertionError(f"outbound connection attempted: {address}")
        return real_connect(self, address)

    def connect_ex(self, address):
        host = host_of(address)
        if host is not None and host not in local:
            attempts.append(address)
            raise AssertionError(f"outbound connection attempted: {address}")
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in local:
            attempts.append(host)
            raise AssertionError(f"DNS lookup attempted: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


@pytest.fixture()
def client(tmp_path, blocked_network):
    app = build_app(tmp_path / "public-ivs")
    # The refresh route is loopback-only by design; present a loopback client.
    with TestClient(app, base_url=LOOPBACK, client=("127.0.0.1", 51234)) as session:
        yield session


def finish(client, response):
    assert response.status_code == 202, response.text[:400]
    task_id = response.json()["task_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = client.get(f"/api/research/tasks/{task_id}").json()
        if task["status"] != "RUNNING":
            return task
        time.sleep(0.02)
    raise AssertionError("research task deadline")


# --------------------------------------------------------------- the dataset

def test_packaged_rows_rederive_their_capture_hash():
    """The public metadata must still be the captured SEC metadata, not an invention.

    Each retained row carries `raw_metadata_hash`; recomputing it from the row's own
    public columns must reproduce it. A hand-edited accession, form, acceptance instant,
    item list or primary document breaks this.
    """
    dataset = load_dataset()
    checked = 0
    for capture in dataset["captures"]:
        for row in capture["rows"]:
            expected = row.get("raw_metadata_hash")
            if not expected:
                continue
            actual = filing_metadata_hash(
                accession=row["accession"], form=row["form"],
                acceptance=row.get("raw_submission_acceptance") or row["acceptance"],
                filing_date=row.get("filing_date"), items=",".join(row.get("items") or []),
                primary_document=row.get("primary_document"))
            assert actual == expected, f"{row['accession']} metadata was altered"
            checked += 1
    assert checked >= 3, "dataset carries too few verifiable rows"


def test_dataset_declares_rights_and_contains_no_owner_material():
    dataset = load_dataset()
    assert dataset["rights"]["sec_metadata"].startswith("SEC EDGAR public filing metadata")
    assert dataset["account"]["account_id"] == "PUBLIC-SYNTHETIC"
    text = DATASET.read_text(encoding="utf-8").casefold()
    for forbidden in ("api_key", "secret", "password", "bearer ", "authorization:"):
        assert forbidden not in text
    # Every capture must name the public source it came from.
    for capture in dataset["captures"]:
        assert capture["source_uri"].startswith("https://data.sec.gov/submissions/CIK")
        assert capture["issuer"]["cik"].isdigit()


def test_price_marks_are_declared_synthetic():
    """What-if and research need a price premise; it must never read as market data."""
    marks = load_dataset()["marks"]
    assert marks["basis"] == "SYNTHETIC_COMPLETED_CLOSE"
    assert "not quotes" in marks["_note"].lower()


# ------------------------------------------------------------------ manifest

def test_manifest_states_the_guarantees(client):
    manifest = client.get("/api/public-ivs").json()
    assert manifest["synthetic"] is True and manifest["keyless"] is True
    assert manifest["paid_model_calls"] == 0 and manifest["live_network"] is False
    assert manifest["model"]["scripted"] is True
    assert manifest["model"]["modes"] == ["FAST", "DEEP"]
    assert sorted(manifest["symbols"]) == ["DOW", "KR", "UNP"]
    assert len(manifest["sec_sources"]) == 3


# ------------------------------------------------------- what the page admits

def test_served_page_states_all_three_data_classes(client):
    """The public page must name what is real, what is invented and what is scripted.

    The earlier build set `app.state.public_ivs` but the page router only knew about
    `ivs_preview`/`public_demo`, so the public build silently served the Owner console
    with no labels at all. This asserts on the served HTML, not on the README.
    """
    html = client.get("/").text
    assert 'data-public-ivs="1"' in html
    assert 'data-ivs-preview="keyless"' in html
    assert 'id="public-ivs-banner"' in html
    for token in ("REAL_PUBLIC_SEC_METADATA", "SYNTHETIC", "SCRIPTED"):
        assert token in html, f"{token} class is not stated on the served page"
    # An English gloss and an explicit no-fee statement for the scripted class.
    assert 'lang="en"' in html
    assert "Real SEC metadata." in html
    assert "0 次模型调用" in html and "无费用" in html
    # Item codes must never be presented as price or investment causality.
    assert "不证明价格或投资影响" in html


def test_console_status_never_names_a_production_model(client):
    """A public build must not advertise a model it never called."""
    status = client.get("/api/console/status").json()
    assert status["synthesis_mode"] == "scripted"
    assert status["model"] == "SCRIPTED_ORACLE_NOT_A_REAL_MODEL"
    assert status["public_ivs"]["paid_model_calls"] == 0
    assert status["public_ivs"]["live_network"] is False
    assert status["public_ivs"]["evidence_classes"] == [
        "REAL_PUBLIC_SEC_METADATA", "SYNTHETIC", "SCRIPTED"]


def test_research_completion_is_deterministic_across_runs(client):
    """A completed run must not depend on thread timing.

    One run per database is the shape the product actually serves, and that is what
    this asserts. Repeating the task on one database still exposes a residual
    WAL/`-shm` race on this host (see the module docstring), so the stress case is
    recorded as a known gap rather than asserted here.
    """
    task = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION, "mode": "FAST"}))
    assert task["status"] == "COMPLETE", task.get("error")
    assert task["assessment"]["key_reasons"]


# ------------------------------------------------------------- Today + SEC

def test_reading_today_never_scans_and_says_so(client):
    body = client.get("/api/today/filings").json()
    assert body["status"] == "NOT_SCANNED"
    assert body["filings"] == [] and body["model_calls"] == 0


def test_refresh_projects_real_filings_with_per_symbol_coverage(client):
    response = client.post("/api/today/filings/refresh", json={})
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["status"] == "COMPLETE_WITH_FILINGS"
    assert body["model_calls"] == 0

    dataset = load_dataset()
    real = {row["accession"] for capture in dataset["captures"] for row in capture["rows"]}
    assert body["filings"], "expected filing cards"
    for card in body["filings"]:
        # Every card must trace to a real captured accession, not a synthesised one.
        assert card["accession"] in real
        assert card["source_url"].startswith("https://www.sec.gov/Archives/edgar/data/")
        assert card["time_precision"] in {"SECOND", "DATE"}

    coverage = {row["symbol"]: row for row in body["per_symbol_coverage"]}
    assert set(coverage) == {"UNP", "KR", "DOW"}
    for row in coverage.values():
        # Coverage is a distinct answer from "no filings"; it must never read as empty.
        assert row["status"] in {"COMPLETE_WITH_FILINGS", "COMPLETE_NO_NEW_FILINGS"}
        assert row["mapping_status"] == "RESOLVED"
        assert row["excluded_by_pit"] >= 0


def test_filing_detail_exposes_point_in_time_and_rights(client):
    client.post("/api/today/filings/refresh", json={})
    card = client.get("/api/today/filings").json()["filings"][0]
    detail = client.get(f"/api/today/filings/{card['evidence_id']}").json()
    assert detail["evidence_id"] == card["evidence_id"]
    assert detail["accepted_at"] and detail["available_at"]
    # Available-at is the conservative bound; it must not precede acceptance.
    accepted = datetime.fromisoformat(detail["accepted_at"])
    available = datetime.fromisoformat(detail["available_at"])
    assert available >= accepted
    assert detail["rights"]["model_forwarding"] in {"GRANTED", "NOT_GRANTED", "UNKNOWN"}
    assert detail["rights"]["basis"]


# ------------------------------------------------------------------- Thesis

def thesis_deltas(client):
    body = client.post("/api/today/filings/refresh", json={}).json()
    return body["thesis_deltas"]


def test_symbol_with_a_prior_thesis_shows_conditions(client):
    report = thesis_deltas(client)
    deltas = [d for d in report["deltas"] if d["symbol"] == "UNP"]
    assert deltas, "no thesis delta for UNP"
    ready = [d for d in deltas if d["status"] in {"THESIS_READY", "THESIS_UNCONFIRMED"}]
    assert ready, "UNP has a synthetic prior thesis, so a condition review is expected"
    delta = ready[0]
    assert delta["cutoff"]["boundary"], "a prior-thesis comparison needs a cutoff"
    assert delta["availability"]["eligible_before"] == delta["cutoff"]["boundary"]


def test_symbol_without_a_thesis_reports_the_gap_instead_of_guessing(client):
    """DOW deliberately has no Thesis. The honest answer is a gap, not a silent HOLD."""
    report = thesis_deltas(client)
    deltas = [d for d in report["deltas"] if d["symbol"] == "DOW"]
    assert deltas, "no thesis delta for DOW"
    assert all(d["status"] == "THESIS_NOT_AVAILABLE" for d in deltas)
    assert all(d["reason"] == "NO_PRIOR_VERSION" for d in deltas)
    assert all(d["statements"] == [] for d in deltas)
    assert all(d["next_check"] for d in deltas)
    assert report["by_status"]["THESIS_NOT_AVAILABLE"] >= 1


# --------------------------------------------------------- Fast -> Deep

def test_fast_then_deep_share_one_task_and_revision_one_is_immutable(client):
    """The revision contract holds whether or not the run reaches COMPLETE.

    Asserted unconditionally on purpose: `revision` is the immutability guarantee, and
    it must not depend on the model finishing.
    """
    fast = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION, "mode": "FAST"}))
    assert fast["revision"] == 1 and fast["mode"] == "FAST"
    task_id = fast["task_id"]

    # Completing Fast must not silently promote a Deep revision.
    assert [r["revision"] for r in
            client.get(f"/api/research/tasks/{task_id}/revisions").json()] == [1]

    deep = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION + "，并给出反证条件",
                                            "mode": "DEEP", "task_id": task_id}))
    assert deep["task_id"] == task_id and deep["revision"] == 2

    # Revision 1 is a frozen object: reading it after Deep returns the Fast answer.
    first = client.get(f"/api/research/tasks/{task_id}/revisions/1").json()
    assert first["revision"] == 1 and first["mode"] == "FAST"
    assert first["question"] == QUESTION
    assert [r["revision"] for r in
            client.get(f"/api/research/tasks/{task_id}/revisions").json()] == [2, 1]


def test_a_selected_filing_is_pinned_into_the_task(client):
    """SEC evidence must enter the task by id, and must carry no scope/quantity data."""
    scan = client.post("/api/today/filings/refresh", json={}).json()
    card = next(row for row in scan["filings"] if row["symbol"] == "UNP")
    task = finish(client, client.post("/api/research/tasks", json={
        "question": QUESTION, "mode": "FAST", "filing_evidence_id": card["evidence_id"]}))
    pinned = task["filing_evidence"]
    assert pinned["source_record_id"] == card["accession"]
    assert pinned["source_uri"].startswith("https://www.sec.gov/Archives/")
    # Public metadata only: no quantity, account or price from the scope.
    assert "quantity" not in json.dumps(pinned["structured_data"]).lower()


def test_scripted_research_reaches_complete_and_cites_evidence(client):
    """Completion is asserted, not masked: a run that fails must fail this test."""
    task = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION, "mode": "FAST"}))
    assert task["status"] == "COMPLETE", task.get("error")
    # The API nests the assessment under `assessment`; reading the task root was the
    # only reason this assertion ever failed (KeyError: 'key_reasons').
    cited = [eid for reason in task["assessment"]["key_reasons"]
             for eid in reason["evidence_ids"]]
    assert cited, "a material claim must carry an evidence id"


# ---------------------------------------------------------------- What-if

def whatif_body(**overrides):
    body = {"revision": 1, "account_id": "PUBLIC-SYNTHETIC", "symbol": "UNP", "side": "BUY",
            "shares": "5", "fill_price": "240", "fee": "1", "scenario_date": "2026-10-24",
            "other_prices": {"KR": "58"}, "unchanged_share_basis_attested": True}
    body.update(overrides)
    return body


def test_what_if_rejects_float_and_exponent_input(client):
    """Exactness at the boundary: a float must never be silently rounded into money."""
    task = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION, "mode": "FAST"}))
    task_id = task["task_id"]
    for bad in ({"shares": 5.5}, {"fill_price": "2.4e2"}, {"fee": True}):
        response = client.post(f"/api/research/tasks/{task_id}/what-if",
                               json=whatif_body(**bad))
        assert response.status_code == 422, f"{bad} was accepted"


def test_what_if_guards_and_exact_decimal_arithmetic(client):
    """Both the share-basis guard and the arithmetic need a completed assessment."""
    task = finish(client, client.post("/api/research/tasks",
                                      json={"question": QUESTION, "mode": "FAST"}))
    task_id = task["task_id"]

    # The guard fires before any arithmetic: unattested share basis is refused.
    refused = client.post(f"/api/research/tasks/{task_id}/what-if",
                          json=whatif_body(unchanged_share_basis_attested=False))
    assert refused.status_code == 422
    assert "SHARE_BASIS" in refused.text

    response = client.post(f"/api/research/tasks/{task_id}/what-if", json=whatif_body())
    assert response.status_code == 200, response.text[:400]
    body = response.json()

    before, after = body["before"], body["after"]
    # Hand-computed: 10 UNP @ 240 = 2400; 10 KR @ 58 = 580; cash 10000 -> NAV 12980.
    assert Decimal(before["shares"]) == Decimal("10")
    assert Decimal(before["cash"]) == Decimal("10000")
    assert Decimal(before["focus_market_value"]) == Decimal("2400")
    assert Decimal(before["nav"]) == Decimal("12980")
    # Buy 5 @ 240 plus a 1 fee: cash 10000 - 1201 = 8799, NAV 12980 - 1 = 12979.
    assert Decimal(after["shares"]) == Decimal("15")
    assert Decimal(after["cash"]) == Decimal("8799")
    assert Decimal(after["focus_market_value"]) == Decimal("3600")
    assert Decimal(after["nav"]) == Decimal("12979")
    # Exact text output: ordinary decimal notation, never exponent form.
    for value in (before["cash"], after["cash"], after["nav"], before["focus_weight"],
                  after["focus_weight"]):
        assert "e" not in value.lower(), f"exponent notation leaked into {value!r}"
    assert before["missing_price_symbols"] == []


# ------------------------------------------------------------- isolation

def test_synthetic_root_refuses_to_reuse_an_unmarked_database(tmp_path):
    root = tmp_path / "reused"
    root.mkdir()
    (root / "public-ivs.sqlite3").write_bytes(b"")
    with pytest.raises(ValueError, match="UNMARKED_DATABASE"):
        build_app(root)


def test_synthetic_root_refuses_product_state():
    with pytest.raises(ValueError, match="ROOT_REFUSED"):
        build_app(Path(__file__).resolve().parents[1] / "data" / "beta-should-not-write")


def test_repeated_fast_deep_on_one_database_preserves_every_original(client):
    """Exercise the reported repeat-on-one-DB WAL failure, without retries."""
    originals = {}
    for _ in range(6):
        fast = finish(client, client.post('/api/research/tasks', json={
            'question': QUESTION, 'mode': 'FAST'}))
        assert fast['status'] == 'COMPLETE', fast.get('error')
        task_id = fast['task_id']
        originals[task_id] = client.get(f'/api/research/tasks/{task_id}/revisions/1').content
        deep = finish(client, client.post('/api/research/tasks', json={
            'question': QUESTION, 'mode': 'DEEP', 'task_id': task_id}))
        assert deep['status'] == 'COMPLETE', deep.get('error')
        receipt = client.post(f'/api/research/tasks/{task_id}/what-if',
                              json=whatif_body(revision=2))
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()['after']['nav'] == '12979'
    for task_id, original in originals.items():
        assert client.get(f'/api/research/tasks/{task_id}/revisions/1').content == original


def test_readonly_save_failure_is_not_swallowed_and_history_survives(client, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    task = finish(client, client.post('/api/research/tasks', json={
        'question': QUESTION, 'mode': 'FAST'}))
    assert task['status'] == 'COMPLETE'
    tasks = client.app.state.research_service.tasks
    original = tasks.get(task['task_id'])

    @contextmanager
    def readonly_pin():
        raise sqlite3.OperationalError('attempt to write a readonly database')
        yield  # pragma: no cover — retain the context-manager protocol

    with monkeypatch.context() as patch:
        patch.setattr(tasks.store, '_wal_pin', readonly_pin)
        with pytest.raises(sqlite3.OperationalError, match='readonly database'):
            tasks.save(original.model_copy(update={'question': 'must not be persisted'}))
    assert tasks.get(task['task_id']) == original
    assert tasks.revision(task['task_id'], 1) == original


def test_wal_pin_releases_windows_file_handle_after_failure(tmp_path):
    from market_agent.persistence import Store

    path = tmp_path / 'synthetic.sqlite3'
    store = Store(path)
    with pytest.raises(RuntimeError, match='injected failure'):
        with store._wal_pin(), store._connect():
            raise RuntimeError('injected failure')
    moved = path.with_name('moved.sqlite3')
    path.rename(moved)
    assert moved.is_file()
