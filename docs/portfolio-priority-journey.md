# Portfolio Priority & Capital Efficiency Journey

An integrated, keyless journey in the existing ConvictionDesk Console. Four synthetic
accounts lead to a deterministic research queue, historical public SEC evidence,
one saved Fast → Deep Task and an account-local Decimal What-if. The existing
published demo, Hero, license and database schema are retained. The v0.12.0 source
candidate connects this journey to the default server's navigation; it does not
claim that a new release has been published or a real model has been verified.

## Install and run

Default product: `python -m market_agent.serve`, open http://127.0.0.1:8080/,
then choose **组合研究优先级**. **返回只读示例** selects the original replay without
mixing its state with this journey. Native interactive requests remain loopback
only. The standalone command below remains available for an isolated preview.

From this candidate's source directory, with Python 3.12 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install .
.\.venv\Scripts\python -m market_agent.portfolio_priority_journey --root output/priority-synthetic --port 8793
```

Open [the local Console](http://127.0.0.1:8793). No key, account import, browser
identity, model provider or external data access is required. The server binds
loopback and refuses port 8000 and occupied ports. Choose a new, dedicated empty
state directory. A matching marked directory supports restart; unrelated state,
changed datasets and linked data/trace files are refused. Keep the directory to
retain research versions and execution receipts. Do not commit generated state.

The legacy application module creates an app when imported. This opt-in factory
bootstraps that import with an empty environment and a separate synthetic SQLite
file in the supplied directory; it never adopts the host's default DB or keys.
The actual application receives explicit local providers and BASELINE strategy.
It has no Adaptive Parser, provider setup, credentials, paid dispatch or order API.

## Walk the complete journey

1. Expand the four-account ledger. It shows cash, shares, average costs, synthetic
   marks, the snapshot time and the mark time. AAPL has no mark; the core
   account's synthetic NAV and every account's current NAV remain UNKNOWN.
2. Read J2, “我应该优先研究哪个持仓？” All six held securities rank by total
   committed cost (`shares × average cost`) descending, with symbol as the tie
   breaker. APP ranks first at USD 5200 across two accounts. This is original
   capital committed, not current market exposure, research alpha or a sizing rule.
   Coverage is shown separately: synthetic marks 5/6, SEC metadata 4/6, comparable
   financials 1/6, current real quotes 0/6. No historical source is labelled fresh.
3. Select APP and explicitly choose `SYN-GROWTH`. Other accounts' cash cannot fund
   it. Start Fast: the existing ResearchService saves an immutable scoped revision
   with historical metadata and the original source identity.
4. Continue Deep on the same Task. It actually runs `sec.fundamentals` and reads
   APP's four comparable YTD facts from accession `0001751008-26-000059`.
   Revenue and OCF each compare January–June 2025 against January–June 2026, USD,
   whole filing entity. Python Decimal derives 55.77% and 34.70%. This is not a
   single-quarter comparison, cash-flow forecast, fair value or current market cap.
5. Read support, counterevidence and the next conditional check. Revenue and OCF
   both increased; revenue grew faster, leaving cash conversion to investigate.
   No confirmed prior Thesis exists. First revision says NO_PRIOR_VERSION; Deep
   identifies the saved Fast revision and new evidence. A direction remains
   UNDETERMINED, never automatic HOLD. All prose is SCRIPTED / NOT_LIVE_MODEL.
6. In the existing single-account What-if form, enter 5 shares, fill 200, fee 1,
   UNP mark 240, date 2026-10-30 and the explicit unchanged-share-basis attestation.
   Cash 1200 → 199, APP shares 30 → 35, hypothetical NAV 8400 → 8399. The receipt
   includes weights and concentration. Increasing to 7 shares is refused despite
   cash elsewhere. Neither path creates an order or changes actual snapshots.
7. Open execution Trace and history, then refresh/restart. Each operation retains
   its recorded tools, state, evidence IDs, PIT and code identity. Reading is GET
   only. A failed research revision is retained; only an explicit retry creates a
   subsequent version. A failed overview read has a read-only retry button.

UNP/KR/DOW offer historical metadata only. Their Deep path reports additional
metadata without claiming comparable financial gain. AAPL/NVDA remain ranked
but research refuses with PUBLIC_SEC_NOT_COVERED. No fabricated live research is
substituted for these missing sources.

## Provenance and limitations

The packaged `portfolio-priority-v1.json` contains only newly authored synthetic
accounts/marks and bounded excerpts of existing public SEC captures: two APP
metadata rows, four rows each for UNP/KR/DOW and four APP XBRL facts. There are no
news bodies, filing prose, vendor payloads or Owner records. SEC URIs, issuer CIK,
listing identity, accession, period, unit, accepted time, capture time and raw
capture hashes remain present. The dataset's LF-normalized SHA256 is pinned in
the module; its metadata hashes, Accepted receipts, security identity, financial
periods, units and evidence hashes are checked before use.

These are historical captures, not independently re-downloaded source bytes.
The implementation session's two unauthenticated SEC fetches returned 403. Those
failures are not a successful live verification; original raw CompanyFacts bytes
are not included. Re-deriving normalized hashes proves integrity and lineage, not
freshness or independent source authenticity. A future live/source-authenticity
acceptance needs a separately permitted successful original-source read.

The research clock is frozen at 2026-10-30 00:00 UTC. Accepted, capture, holdings,
marks and research cutoffs have distinct meanings. Missing valuation, current
prices, confirmed investor Thesis, current full-portfolio coverage and model
quality remain gaps. This public/synthetic engineering journey is not Owner
Product Acceptance or release approval.

## Reproduce the checks

```powershell
.\.venv\Scripts\python -m pip install '.[test]'
.\.venv\Scripts\python -m pytest tests/test_portfolio_priority_journey.py tests/test_public_ivs_journey.py tests/test_portfolio_whatif.py tests/test_research_execution_trace.py --basetemp=output/priority-test -q
.\.venv\Scripts\python scripts/verify_portfolio_priority.py --output output/priority-runtime
```

Use a fresh output directory for each preserved runtime report. The script starts
its own non-8000 child, proves a process-wide outbound guard, runs 21 HTTP Eval
checks, abruptly stops only that child, restarts it and checks exact saved bytes.
`HTTP_EVAL.json`, `server.log` and `outbound-guard.json` are the artifacts.

With a separately installed Playwright and Chrome, against the running server:

```powershell
node scripts/smoke_portfolio_priority_browser.cjs http://127.0.0.1:8793 output/priority-browser ABSOLUTE_PATH_TO_PLAYWRIGHT_PACKAGE_JSON
```

The script exercises desktop 1440 and mobile 390, source gaps, required account
selection, real saved Fast/Deep, evidence/Trace, the existing What-if form,
insufficient cash, immutable history, GET failure/recovery, reload and dark mode.
It records screenshots and separates deliberately induced HTTP 422/503 console
messages from unexpected errors. It asserts no external requests or page overflow.

## Scoped product-contract delta

| Requirement | This candidate's increment | Remaining gap |
| --- | --- | --- |
| CD-004 | Actual same-Task scripted Fast metadata → Deep comparable financial read | Live-model quality and adaptive investigation unverified |
| CD-008 | Same-accession/period/unit YTD facts and exact Decimal comparison | Current valuation and full four-quarter coverage absent |
| CD-013 | Wheel-installable keyless opt-in journey, historical capture disclosure | Independent Claude review, integration, final QA and release gates separate |
| CD-015 | Cost-priority queue with scoped evidence and saved revision delta | Portfolio-wide current What's Changed / human acceptance unverified |
| CD-016 | Existing single-account share/cash/concentration engine exercised | No trading, sizing policy or current quote qualification |
| CD-021 | Four-account disclosure, explicit research account, no cash pooling | General live cross-account decision quality unverified |

Global requirement statuses are not promoted here. The private ledger is not part
of the allowlisted public base; its sole Integrator receives this exact scoped
delta for controlled incorporation. Historical decisions and failed verdicts remain.
