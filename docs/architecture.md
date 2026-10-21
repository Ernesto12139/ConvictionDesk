# Architecture

ConvictionDesk is a single Python service — FastAPI, Pydantic v2, `httpx` and stdlib `sqlite3` — with a dependency-free HTML/CSS/JS console. It has no agent framework, vector database or queue. The design goal is narrow: every saved judgment must be traceable to the holdings, evidence and time it was made with, and every number in it must come from code.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-architecture-dark.svg">
    <img alt="Layered architecture. 1 Sources: company news (Finnhub), SEC EDGAR filings and XBRL, end-of-day prices (Massive), portfolio snapshot. 2 Typed evidence and point-in-time: evidence identity, cutoff, freshness and session, deduplication. 3 Deterministic finance in Decimal: portfolio analytics, SEC quarters, numeric guard. 4 Attention and orchestration: Today scan, research task, model profiles. 5 Fast or Deep decision with citation validation. 6 Action and thesis delta. 7 Decision record: immutable revisions, reopen and audit, keyless replay. Dashed planned boxes: 13F and Form 4, live EV and multiples with scenarios, cross-provider BYOK." src="assets/convictiondesk-architecture-light.svg" width="100%">
  </picture>
</p>

Dashed boxes are planned work.

## The research loop

A research task moves through five stages. The tool order is owned by code; the model decides only what it is asked to decide, inside fixed budgets.

1. **Scope.** The task freezes a portfolio snapshot, the selected account and a cutoff time. A follow-up keeps the same snapshot and account, so a thread of research never silently changes its premises.
2. **Gather.** Typed tools read the portfolio, end-of-day prices, the latest thesis, the previous decision, company news and SEC filings — plus, in Deep, exact-period SEC financials. Every tool call is recorded with its arguments, status and output IDs.
3. **Assess.** A model call receives a bounded projection of the evidence and returns a typed `ResearchAssessment`: a direct judgment, reasons, support, counterevidence, change conditions, next checks and an Action.
4. **Check.** In Deep, the assessment may name one evidence gap that could change the judgment. At most one targeted news or SEC search runs; the reassessment must state whether the new evidence changed the call, and a claimed change must cite the new evidence.
5. **Validate and save.** The output is checked before anything is written. A passing result becomes an immutable revision; a failing one is saved as a failed round with its reason, without a new judgment.

Code: [`research_task.py`](../src/market_agent/research_task.py) (`ResearchService.execute`, `validate_assessment`, `ResearchTasks`), [`research_context.py`](../src/market_agent/research_context.py) (`build_context`).

## Fast and Deep

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-fast-deep-dark.svg">
    <img alt="Fast versus Deep. Both start from a ranked Today item or a question with the same holding, account scope and time cutoff, and both end in the same typed Action Decision. Fast: up to 8 sources and 12 KB, one judgment, up to 3,072 output tokens and 45 seconds. Deep: up to 18 sources and 24 KB, names the most material gap and runs one targeted news or SEC check, adds up to four SEC quarters in Decimal, up to 8,192 output tokens and 90 seconds, reports NEW_EVIDENCE or NO_NEW_EVIDENCE." src="assets/convictiondesk-fast-deep-light.svg" width="100%">
  </picture>
</p>

| | **Fast** | **Deep** |
|---|---|---|
| Question it answers | "Does this news change anything for me?" | "Should I act before the next report?" |
| Context | Up to 8 qualified sources, 12 KB | Up to 18 qualified sources, 24 KB, SEC quarters and trailing-twelve-month figures |
| Extra work | None | One targeted news or SEC search on the most material gap |
| Model budget | ≤ 3,072 output tokens, 45 s per call | ≤ 8,192 output tokens, 90 s per call |
| Result | Typed Action Decision | The same, plus `NEW_EVIDENCE` or `NO_NEW_EVIDENCE` |

Deep looks further; when its search finds nothing new, it says so.

## From evidence to action

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-evidence-to-action-dark.svg">
    <img alt="From evidence to action. An unconfigured prompt goes straight from question to fluent answer, without supplied context, an enforced cutoff, checked arithmetic or a saved revision. ConvictionDesk goes in six steps: new market event, qualified evidence published before the cutoff, thesis relevance tied to your holding and account, material change, action direction with status and evidence strength, and what would change my mind. When evidence is too thin the result is UNDETERMINED, never a default HOLD; REDUCE and EXIT need a real position. Every result is saved as an immutable, hash-checked revision." src="assets/convictiondesk-evidence-to-action-light.svg" width="100%">
  </picture>
</p>

## Design principles

### Point in time

Publication time, retrieval time, financial period and price session are separate fields. A company source must belong to the focus symbol and be published before the cutoff; a source that fails is rejected at the tool boundary, before it can be saved or cited. Stale company receipts stay out of current-event context, old stories are never relabelled as today's, and stale facts may be cited only as history.

### Deterministic finance

Portfolio analytics, SEC quarter and year-to-date normalization (including derived Q4 flows), trailing-twelve-month revenue, net income and operating cash flow, and the account What-if all run in Python `Decimal`. EPS is never derived by subtraction. Each figure keeps its unit, currency, period, as-of time and source. Code: [`financial_series.py`](../src/market_agent/financial_series.py), [`sec.py`](../src/market_agent/sec.py), [`portfolio_whatif.py`](../src/market_agent/portfolio_whatif.py).

### Unknown stays unknown

A missing price is unknown, not zero, and weights and NAV that depend on it are unknown too. Market capitalization and valuation multiples stay unknown without complete share-class and corporate-action qualification.

### Typed statements and validation

Every statement in an answer carries an identity — `VERIFIED_FACT`, `USER_ASSERTION`, `OWNER_CONFIRMED_THESIS`, `HISTORICAL_DECISION`, `AI_DRAFT` or `UNKNOWN` — and cites evidence IDs. Before saving, `validate_assessment` rejects:

- citations to evidence that was not supplied in this task;
- numbers or units that do not appear in the cited sources;
- exact buy or sell quantities, which the model may not prescribe;
- a statement whose identity does not match its sources, such as a user assertion presented as a verified fact;
- current claims supported only by stale evidence.

The Action itself is checked by [`action_decision.py`](../src/market_agent/action_decision.py): a direction requires current company evidence, insufficient evidence must be `UNDETERMINED`, and `HOLD`, `REDUCE` and `EXIT` read back as not applicable without a real position.

### Immutable history

Each research round is a revision stored with its content hash. Follow-ups append a new revision; the current pointer moves, earlier revisions stay byte-identical. Schema upgrades are tested against frozen payloads of earlier versions.

## Read-only MCP

An optional [Model Context Protocol](https://modelcontextprotocol.io/) client ([`longbridge_mcp.py`](../src/market_agent/longbridge_mcp.py)) speaks JSON-RPC 2.0 over Streamable HTTP and SSE. It discovers tools, keeps only an allowlist of read-only research tools, and refuses any other tool name before touching the network — even if the server advertises trading. Calls run under deadlines and size limits, and quotes are parsed as `Decimal`. It is off unless `MIA_RESEARCH_MCP=readonly` is set, and the research loop works without it.

## Today

Today ([`attention.py`](../src/market_agent/attention.py)) scans company news for your research universe on demand. It removes duplicates and stories outside the current window, preselects candidates, screens them in one bounded model batch and ranks the results deterministically. Each scan reports whether it was complete, found nothing material, ended partial under provider limits, or could not reach a provider.

## Evaluation

The repository evaluates behavior without keys or network:

- a scripted oracle model drives the real research service end to end ([`evals/research_agent.py`](../evals/research_agent.py));
- the keyless replay serves a hash-checked fictional dataset, and HTTP and Playwright journeys walk Today → Fast / Deep → Action → Evidence → Records on every commit ([`scripts/smoke_public_demo.py`](../scripts/smoke_public_demo.py), [`scripts/smoke_public_demo_browser.cjs`](../scripts/smoke_public_demo_browser.cjs));
- the deterministic suite covers decision contracts, citation and point-in-time rejection, `Decimal` finance, Today states, the What-if and replay integrity ([`tests/`](../tests/)).

These checks show the pipeline behaves as specified. What they cover, and what they do not, is listed in [Verification](verification.md).
