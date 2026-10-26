"""Keyless, offline vertical slice: real public SEC metadata to a decision.

This is the smallest complete journey the product can honestly show a stranger:

    real SEC filing metadata -> Today card -> Thesis condition or explicit gap
        -> scripted Fast then Deep research on one immutable revision
        -> deterministic What-if arithmetic

What is real: the filing metadata. Every accession, acceptance instant, form type and
Archives URL in the packaged dataset was read from SEC EDGAR and carries the capture
hash it was read under, so a reviewer can open the document and check it.

What is synthetic: the account, the quantities and the Theses. They are labelled
`PUBLIC-SYNTHETIC` / `SYNTHETIC` everywhere they surface. No Owner position, account
identity, note, credential or licensed news body is present, and the packaged dataset
is the only data source.

What never happens at runtime: a model call, a network request, or a paid API. The
research modes are served by a scripted oracle, so Fast and Deep are demonstrable
without a key while still producing the real immutable-revision semantics.
"""
from __future__ import annotations

import json

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

DATASET = Path(__file__).with_name("demo_assets") / "public-ivs-v1.json"
MARKER = "PUBLIC_IVS_SYNTHETIC_ONLY.json"
SCHEMA = "public-ivs-v1"


def load_dataset(path: Path | None = None) -> dict:
    """Read the packaged dataset. This file is the only permitted data source."""
    source = Path(path) if path else DATASET
    dataset = json.loads(source.read_text(encoding="utf-8"))
    if dataset.get("schema_version") != SCHEMA:
        raise ValueError(f"unsupported public IVS dataset schema: {dataset.get('schema_version')!r}")
    symbols = {capture["issuer"]["symbol"] for capture in dataset["captures"]}
    if not symbols:
        raise ValueError("dataset has no SEC captures")
    return dataset


def _refuse_unsafe_root(root: Path) -> None:
    """Never write a synthetic database on top of a real one.

    An Owner database, the canonical checkout's `data/` directory or a path that is not
    a fresh directory are all refused, so a demo run cannot adopt existing records.
    """
    root = root.resolve()
    package = Path(__file__).resolve().parents[2]
    for guarded in (package / "data", package / "src"):
        if root == guarded or guarded in root.parents:
            raise ValueError(f"PUBLIC_IVS_ROOT_REFUSED: {root} is inside product state")


def build_app(root: Path | str, *, dataset: dict | None = None,
              clock=None) -> "object":
    """Compose the real Console over the packaged dataset. No key, no network."""
    root = Path(root).resolve()
    _refuse_unsafe_root(root)
    root.mkdir(parents=True, exist_ok=True)
    database = root / "public-ivs.sqlite3"
    marker = root / MARKER
    if database.exists() and not marker.exists():
        # An unmarked database may hold someone else's records; refuse rather than adopt.
        raise ValueError("PUBLIC_IVS_UNMARKED_DATABASE_REFUSED")

    from .llm import LLMError
    from .main import create_app
    from .news import NewsResult
    from .persistence import Store
    from .portfolio_entry import PortfolioEntry
    from .provider import ProviderError, UnconfiguredEODProvider
    from .research_universe import ResearchStore, UniverseChange
    from .sec import SECSearchResult
    from .sec_filings import CapturedFilingsSource
    from .tdb import TDBError
    from .thesis import (ThesisDraftOutput, ThesisGenerationMetadata, ThesisProfile,
                         ThesisStatement, ThesisStatus)

    data = dataset or load_dataset()
    now = datetime.fromisoformat(data["as_of"]) if clock is None else clock()
    captures = {c["issuer"]["symbol"]: c for c in data["captures"]}
    sources = {symbol: CapturedFilingsSource(c) for symbol, c in captures.items()}
    ciks = {symbol: c["issuer"]["cik"] for symbol, c in captures.items()}

    class FrozenSEC:
        """Replays the packaged captures; refuses anything the dataset does not hold."""
        name = "sec_edgar_public_capture"

        def recent_filings(self, symbol, *, as_of):
            if symbol not in sources:
                raise ProviderError("not_found", "Symbol is not in the packaged public sample")
            return sources[symbol].recent_filings(symbol, as_of=as_of)

        def search(self, symbol, query, forms, as_of, limit=5, **kwargs):
            return SECSearchResult(cik=ciks.get(symbol, ""), indexed_documents=0, evidence=[])

        fast_search = search
        targeted_search = search

        def fundamentals(self, *args, **kwargs):
            raise ProviderError("not_found",
                                "Frozen metadata has no financial statements by design")

    class NoNews:
        name = "public-ivs-no-news"

        def search(self, *args, **kwargs):
            return NewsResult(evidence=[], coverage_complete=False)

    from decimal import Decimal

    from .domain import EvidenceItem, PriceObservation
    from .provider import MarketDataResult
    from .tdb import content_hash

    marks = {symbol: Decimal(text)
             for symbol, text in data["marks"]["prices"].items()}

    class SyntheticMarks:
        """Declared synthetic closes. Not a quote, not a provider, not a claim.

        Research needs a price premise and What-if needs a reference mark; without one
        the task fails with RESEARCH_FAILED. These numbers are invented, labelled
        SYNTHETIC in the evidence payload, and never presented as market data.
        """
        name = "public-ivs-synthetic-marks"

        def retrieve_eod(self, symbols, cutoff):
            observations, evidence = [], []
            for symbol in symbols:
                price = marks.get(symbol)
                if price is None:
                    continue  # unknown stays unknown; never defaulted to zero
                payload = {"price": format(price, "f"), "basis": "SYNTHETIC_COMPLETED_CLOSE",
                           "synthetic": True, "note": "SYNTHETIC MARK, NOT A QUOTE"}
                digest = content_hash(payload)
                stamp = now - timedelta(days=1)
                source = EvidenceItem(
                    evidence_id="E" + digest[7:23], evidence_type="market_data",
                    provider="synthetic_marks", symbol=symbol,
                    title="SYNTHETIC 价格标记（不是行情）",
                    source_uri="https://example.com/convictiondesk-synthetic/marks",
                    published_at=stamp, as_of=stamp, retrieved_at=stamp,
                    content_hash=digest, structured_data=payload)
                evidence.append(source)
                observations.append(PriceObservation(
                    symbol=symbol, price=price, as_of=stamp,
                    provider=source.provider, evidence_id=source.evidence_id))
            return MarketDataResult(observations=observations, evidence=evidence)

    from .agent import MockLLMClient

    class ScriptedOracle(MockLLMClient):
        """Deterministic stand-in for a model. Cannot call out, cannot spend."""
        provider = "scripted-local"
        model_name = "SCRIPTED_ORACLE_NOT_A_REAL_MODEL"

        async def complete_structured(self, request):
            if request.purpose == "research.intent":
                symbols = request.facts["explicit_literals"]["symbols"]
                symbol = symbols[0] if symbols else request.facts.get("previous_focus")
                return request.response_model.model_validate(dict(
                    objective="REVIEW_THESIS", focus_symbol=symbol, clarification=None,
                    sec_query="operating results cash flow disclosure",
                    use_news=False, use_sec=True))
            if request.purpose not in {"research.assess", "research.reassess"}:
                raise LLMError("configuration", "Unsupported scripted purpose")
            facts = request.facts["sources"]
            public = [s["evidence"]["evidence_id"] for s in facts
                      if s["identity"] == "VERIFIED_FACT"
                      and s["evidence"]["evidence_type"] == "sec_filing"]
            ids = public[:1] or [s["evidence"]["evidence_id"] for s in facts][:1]

            def statement(text, identity="AI_DRAFT"):
                return {"text": text, "identity": identity, "evidence_ids": ids}

            return request.response_model.model_validate(dict(
                disposition="RESEARCH_ONLY",
                core_judgment=statement("先阅读原始申报，核对与既有论点相关的条件，再决定是否深入研究。"),
                key_reasons=[statement("已保留一条可追溯到 SEC 原文的申报记录。",
                                       "VERIFIED_FACT" if public else "UNKNOWN")],
                support=[],
                counterevidence=[statement("申报元数据尚不能说明经营影响；需要原文和可比较的业务事实。",
                                           "UNKNOWN")],
                action_conditions=[statement("核对报告期、原文业务事实与既有论点条件后，再更新研究判断。")],
                next_checks=[statement("阅读这份原始申报，核查与既有持仓逻辑相关的事实。")],
                supplemental_query=None))

    oracle = ScriptedOracle()

    class ScriptedProfiles:
        """Two real modes over one scripted client, so Fast and Deep both resolve."""
        defaults = {"FAST": "public-fast", "DEEP": "public-deep"}

        def describe(self):
            return [dict(profile_id="public-" + mode.lower(), mode=mode,
                         provider=oracle.provider, model=oracle.model_name, available=True,
                         max_output_tokens=3072 if mode == "FAST" else 8192,
                         timeout_seconds=45 if mode == "FAST" else 90)
                    for mode in ("FAST", "DEEP")]

        def resolve(self, profile_id=None, mode=None):
            profile_id = profile_id or self.defaults[mode or "DEEP"]
            item = next((p for p in self.describe() if p["profile_id"] == profile_id), None)
            if item is None or mode and item["mode"] != mode:
                raise TDBError("RESEARCH_MODEL_PROFILE_INVALID", 422)
            return SimpleNamespace(**item)

        def client(self, profile_id):
            self.resolve(profile_id)
            return oracle

    store = Store(database)
    if store.get_snapshot() is None:
        account = data["account"]
        store.add_snapshot(PortfolioEntry(
            accounts=[dict(account_id=account["account_id"], name=account["name"],
                           cash=account["cash"])],
            positions=[dict(position_id="public-" + p["symbol"],
                            account_id=account["account_id"], symbol=p["symbol"],
                            quantity=p["quantity"], avg_cost=p["avg_cost"])
                       for p in account["positions"]],
        ).snapshot(now - timedelta(days=20)))

    research = ResearchStore(store)
    for symbol in data.get("watchlist", []):
        research.change(UniverseChange(symbol=symbol, state="WATCHING",
                                       reason="PUBLIC SYNTHETIC demo watch scope"),
                        now - timedelta(days=20))

    # Theses are synthetic and dated before the filings, so the "prior knowledge" cutoff
    # is a real comparison rather than a formality.
    for symbol, groups in data.get("theses", {}).items():
        if store.get_thesis(symbol) is not None:
            continue
        as_of = now - timedelta(days=30)
        draft = ThesisDraftOutput(**{
            key: [ThesisStatement(text=text, evidence_ids=["Esyntheticthesisfixture"])
                  for text in texts] for key, texts in groups.items()
        } | {"evidence_ids": ["Esyntheticthesisfixture"]})
        payload = {"symbol": symbol, "as_of": as_of.isoformat(),
                   "draft": draft.model_dump(mode="json"), "model": "synthetic-fixture"}
        from .tdb import content_hash
        digest = content_hash(payload)
        profile = ThesisProfile(**draft.model_dump(), symbol=symbol,
                                thesis_version_id="TV" + digest[7:23], as_of=as_of,
                                status=ThesisStatus.USER_CONFIRMED,
                                generation=ThesisGenerationMetadata(model="synthetic-fixture"))
        from .domain import EvidenceItem
        store.add_thesis(profile, EvidenceItem(
            evidence_id="E" + digest[7:23], evidence_type="thesis",
            provider="synthetic_fixture", symbol=symbol,
            title="SYNTHETIC 投资逻辑（示例，不是 Owner 记录）",
            source_record_id=profile.thesis_version_id,
            retrieved_at=as_of + timedelta(minutes=5), as_of=as_of, content_hash=digest,
            structured_data=profile.model_dump(mode="json")))

    app = create_app(database, environment={"MIA_RESEARCH_ADAPTIVE": "1"}, clock=lambda: now,
                     market_provider=SyntheticMarks(), llm_client=oracle,
                     sec_provider=FrozenSEC(), news_provider=NoNews(),
                     model_profiles=ScriptedProfiles())
    manifest = {
        "schema_version": SCHEMA,
        "kind": data["kind"],
        "as_of": now.isoformat(),
        "synthetic": True,
        "keyless": True,
        "paid_model_calls": 0,
        "live_network": False,
        "model": {"provider": oracle.provider, "name": oracle.model_name,
                  "modes": ["FAST", "DEEP"], "scripted": True},
        "symbols": sorted(captures),
        "holdings": [p["symbol"] for p in data["account"]["positions"]],
        "watchlist": list(data.get("watchlist", [])),
        "theses": sorted(data.get("theses", {})),
        "thesis_gap": data.get("thesis_note"),
        "sec_sources": data["capture_sources"],
        "rights": data["rights"],
        "data_source": "Packaged SEC EDGAR public metadata capture; no live read.",
    }
    app.state.public_ivs = manifest
    app.get("/api/public-ivs")(lambda: app.state.public_ivs)
    marker.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return app


def journey(root: Path | str, *, question: str = "UNP 最新季报与既有投资逻辑的关系",
            dataset: dict | None = None) -> dict:
    """Run the entire slice once and return the evidence a reviewer needs.

    Kept as a function rather than only a test so a clean clone can reproduce the claim
    with one command, and so the end-to-end run happens in a fresh process instead of
    inheriting a test session's state.
    """
    import time

    from fastapi.testclient import TestClient

    app = build_app(root, dataset=dataset)
    base = "http://127.0.0.1:8791"

    def finish(client, response):
        if response.status_code != 202:
            raise RuntimeError(f"task not accepted: {response.status_code} {response.text[:300]}")
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            task = client.get(f"/api/research/tasks/{task_id}").json()
            if task["status"] != "RUNNING":
                return task
            time.sleep(0.02)
        raise RuntimeError("research task did not finish")

    with TestClient(app, base_url=base, client=("127.0.0.1", 51234)) as client:
        before_scan = client.get("/api/today/filings").json()
        scan = client.post("/api/today/filings/refresh", json={}).json()

        deltas = scan.get("thesis_deltas", {}).get("deltas", [])
        thesis = {}
        for delta in deltas:
            thesis.setdefault(delta["symbol"], {"status": delta["status"],
                                                "reason": delta.get("reason"),
                                                "statements": len(delta.get("statements") or [])})

        # Ground the research in a real filing the reader can open, not in a bare question.
        selected = next((row for row in scan.get("filings", [])
                         if row["symbol"] == question.split()[0]), None)
        if selected is None and scan.get("filings"):
            selected = scan["filings"][0]
        selection = {"filing_evidence_id": selected["evidence_id"]} if selected else {}

        fast = finish(client, client.post("/api/research/tasks",
                                          json={"question": question, "mode": "FAST",
                                                **selection}))
        task_id = fast["task_id"]
        deep = finish(client, client.post("/api/research/tasks",
                                          json={"question": question + "，并给出反证条件",
                                                "mode": "DEEP", "task_id": task_id}))
        revisions = [r["revision"] for r in
                     client.get(f"/api/research/tasks/{task_id}/revisions").json()]
        first = client.get(f"/api/research/tasks/{task_id}/revisions/1").json()

        scope_response = client.get(f"/api/research/tasks/{task_id}/what-if",
                                    params={"revision": 1})
        whatif = {"scope_status": scope_response.status_code}
        if scope_response.status_code == 200:
            account = scope_response.json()["accounts"][0]["account_id"]
            response = client.post(f"/api/research/tasks/{task_id}/what-if", json={
                "revision": 1, "account_id": account, "symbol": "UNP", "side": "BUY",
                "shares": "5", "fill_price": "240", "fee": "1",
                "scenario_date": "2026-10-24", "other_prices": {"KR": "58"},
                "unchanged_share_basis_attested": True})
            whatif.update({
                "status": response.status_code,
                "before": response.json().get("before") if response.status_code == 200 else None,
                "after": response.json().get("after") if response.status_code == 200 else None,
                "detail": None if response.status_code == 200 else response.text[:200],
            })
        else:
            # A failed assessment leaves no saved action, so What-if is correctly refused.
            whatif.update({"status": None, "before": None, "after": None,
                           "detail": scope_response.text[:200]})

    return {
        "manifest": app.state.public_ivs,
        "today": {"before_scan": before_scan["status"],
                  "after_scan": scan["status"],
                  "filings": len(scan.get("filings", [])),
                  "model_calls": scan.get("model_calls"),
                  "coverage": {row["symbol"]: row["status"]
                               for row in scan.get("per_symbol_coverage", [])},
                  "accessions": [row["accession"] for row in scan.get("filings", [])[:3]]},
        "thesis": thesis,
        "research": {"fast": {"status": fast["status"], "revision": fast["revision"],
                              "mode": fast.get("mode"), "error": fast.get("error")},
                     "deep": {"status": deep["status"], "revision": deep["revision"],
                              "error": deep.get("error")},
                     "same_task": deep["task_id"] == task_id,
                     "revisions": revisions,
                     "revision_one_preserved": first["revision"] == 1
                                               and first.get("mode") == "FAST",
                     "selected_filing": (fast.get("filing_evidence") or {}).get("source_record_id"),
                     "selected_filing_carried": deep.get("filing_evidence")
                                                == fast.get("filing_evidence"),
                     "cited": sorted({eid
                                      for reason in (fast.get("assessment") or {}).get("key_reasons", [])
                                      for eid in reason.get("evidence_ids", [])})},
        "what_if": whatif,
    }


def main(argv=None):
    import argparse
    import socket

    parser = argparse.ArgumentParser(
        description="Keyless public IVS slice: real SEC metadata -> scripted research -> What-if")
    parser.add_argument("--root", type=Path, default=Path(".public-ivs"),
                        help="directory for the synthetic database (default: ./.public-ivs)")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--serve", action="store_true", help="run the HTTP server")
    parser.add_argument("--journey", action="store_true",
                        help="run the whole slice once and print the evidence as JSON")
    args = parser.parse_args(argv)
    if args.port == 8000:
        raise SystemExit("Refusing port 8000: that listener belongs to the Owner runtime")
    if args.journey:
        print(json.dumps(journey(args.root), indent=2, ensure_ascii=False))
        return 0
    app = build_app(args.root)
    if not args.serve:
        print(json.dumps(app.state.public_ivs, indent=2))
        return 0
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise SystemExit(f"Port {args.port} already in use; refusing to disturb a listener")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
