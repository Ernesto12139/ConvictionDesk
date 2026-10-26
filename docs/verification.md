# Verification

## v0.10.0 public IVS scope

The new keyless entrypoint is `python -m market_agent.public_ivs --serve` on8791:
frozen real SEC metadata → fictional prior Thesis/portfolio → scripted Fast and
same-task Deep → cited evidence/counterevidence → Decimal result-card What-if.
Runtime execution makes no external model/data requests; dependency installation
may require network. There is no live model-quality or investment-accuracy claim.

Public CI retains the earlier replay, SEC authority/time/F2 and history gates,
and adds strict research completion, repeated same-database WAL/failure tests,
import/resource closure, actual desktop/mobile result-card What-if and a
non-root installed Docker journey with network disabled and root filesystem
read-only. The PR jobs checkout the exact PR head, rather than its synthetic
merge object. `release/RELEASE_MANIFEST.json` binds exported file bytes;
`release/IVS_EVIDENCE_INCREMENT_MANIFEST.json` records source and preservation.

The v0.9.0 capability history below describes the retained replay and earlier
engineering scope. It does not turn this scripted demo into live research or
human business acceptance.

This page records how far each capability in v0.9.0 is verified. It is the single place for scope and limits; the README links here instead of repeating them.

**Verified (scoped)** means covered by deterministic tests and the keyless HTTP and browser journeys described below. **Implemented** means the code ships and is exercised on synthetic or frozen inputs, but has not been checked against live providers. **Experimental** means it works with your own keys and has not been benchmarked.

## Capabilities

| Status | Capability |
|---|---|
| Verified (scoped) | Typed `ADD` / `HOLD` / `REDUCE` / `EXIT` with status, evidence strength, thesis delta, counterevidence, change conditions and holding applicability |
| Verified (scoped) | Fast / Deep budgets, context limits and tool permissions |
| Verified (scoped) | Evidence-ID and point-in-time validation: invalid citations and later sources are rejected |
| Verified (scoped) | SEC monetary quarter / year-to-date normalization in `Decimal`, derived Q4 flows; EPS never derived by subtraction |
| Verified (scoped) | Today states — complete, no material events, partial, provider unavailable — and cost confirmation before live work |
| Verified (scoped) | Keyless replay: Today → Fast / Deep → Action → Evidence → Records with zero model and data calls |
| Implemented, synthetic verification | Same-task Fast → Deep continuation |
| Implemented, synthetic verification | Historical trailing-twelve-month figures in Deep's cited context; per-symbol Today coverage; current batch versus older pending receipts |
| Implemented, synthetic verification | Local price-scenario receipt from your dated prices; refuses known share-class or corporate-action conflicts; never rewrites the saved Action |
| Implemented, synthetic verification, independent engineering review | Decision-first research page and single-account What-if: holding-account default, fees, missing prices, cash, oversell and share-class guards, trade versus repricing versus modeled P&L, exact plain decimals |
| Implemented, synthetic verification, independent engineering review | Regular-session OHLCV qualification → SMA20, simple ATR14 and 10-session candidate bands → explicit hypothetical account preview. Real OHLCV is not admitted yet |
| Experimental | Live Today scans, live research and CSV portfolio import with your own keys; other OpenAI-compatible models |

ConvictionDesk makes no accuracy, return or backtest claim. The quality of live screening and of live decisions is not benchmarked.

## Continuous integration

Every push and tag runs the [CI workflow](../.github/workflows/ci.yml):

- the deterministic test suite on Ubuntu and Windows — decision contracts, citation and point-in-time rejection, `Decimal` finance, Today states, the What-if and replay integrity;
- a keyless **HTTP journey** and a **Playwright browser journey** of the replay at desktop and mobile widths in light and dark themes, with every guest write denied and no model calls;
- a **read-only Docker preview** smoke test, running as a non-root user with all capabilities dropped;
- a **public-history audit** of authorship, refs and release identity.

Run the same checks locally:

```bash
pip install ".[test]"
python -m pytest -q
python -m market_agent.serve &        # keyless replay on :8080
python scripts/smoke_public_demo.py --base http://127.0.0.1:8080
```

## Known limits

- Prices are end-of-day closes; there are no real-time quotes.
- A full watch-list Today scan can end partial under provider rate limits, and a large saved queue can exceed one screening batch.
- No live adapter yet qualifies share classes and corporate actions, so live valuation multiples stay unknown.
- There is no broker integration and no order execution.
- The interface is in Simplified Chinese.

## Releases

Each public release is an allowlisted export of one verified upstream commit. The commit, file list and per-file hashes are recorded in [`release/RELEASE_MANIFEST.json`](../release/RELEASE_MANIFEST.json); release notes: [v0.9.0](../release/V09_RELEASE_NOTES.md).
