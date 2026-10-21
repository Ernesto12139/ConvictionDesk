# Saved Action → single-account hypothetical fill (CD-016)

Select a complete saved research revision, choose an eligible account, then enter
BUY/SELL, positive shares, hypothetical USD fill price, fees and scenario date.
Confirm share-basis continuity as an unverified user assumption. Other holding
prices are optional hypothetical marks; none are fetched or silently copied from
a commercial display. Missing prices leave NAV/weights/concentration UNKNOWN.

The identical price vector values both states, so the difference isolates the
fill and fees. Focus value = shares × hypothetical price; cash subtracts buy
notional plus fees or adds sell notional less fees. Cash plus equity gives NAV
only when every remaining holding is priced. Focus/largest weights divide by
account NAV; HHI sums squared equity/NAV weights (cash excluded). This is nominal
single-name exposure, not sector/beta/volatility/stress risk. Zero NAV weights
are undefined. Average-cost modeled P&L is not tax-lot accounting.

`GET /api/research/tasks/{task_id}/what-if?revision=N` lists only accounts permitted
by the pinned research scope. `POST` takes `WhatIfRequest`. The server loads the
exact saved snapshot ID, verifies its content hash and PIT, and checks the task
revision/focus symbol. It refuses oversell, insufficient cash including fees,
unsupported currency, missing share-basis attestation and known split/class
conflicts. An unheld symbol can be bought; it cannot be sold short. There are no
orders, providers, new model inputs, database writes or schema migrations.

The receipt binds the full input, snapshot and task projection hashes, includes
the scenario date separately from snapshot/research times, and keeps the original
Action unchanged. Closing the page discards the detached receipt; saved research
history remains immutable. Download/copying a receipt is not a real transaction.
The saved direction belongs to its original research scope; selecting another
eligible account does not reassess or authorize that direction for that account.

D-080 semantics: both original-share rows are marked at hypothetical fill.
Large unrealized P&L therefore represents existing shares versus average cost,
not profit created by buying. `trade_nav_delta` is always minus fees. Optional
`reference_mark` is another user hypothesis: old quantity × (fill-reference)
is `market_repricing_delta`; plus trade delta gives the hypothetical total.
Without that mark, repricing/total remain unknown. Neither mark is actual market
movement or an actual fill. Fractional shares up to10 decimal places are supported;
scientific notation strings are refused for every amount/mark. No broker
settlement rounding is modeled. Historical v1 detached receipts stay unchanged;
new receipts are v2. Source action/history never changes.

Validation: `tests/test_portfolio_whatif.py` covers synthetic AAPL (consumer
hardware), JPM (financial), SMR (pre-commercial capital-intensive), held/unheld,
exact rational/integer-cent oracles and class/action counterexamples. Synthetic
prices and judgments never establish real valuation or research accuracy.

Today quality (CD-001), real-model gain (CD-004), Portfolio What's Changed
(CD-015), dated OHLCV technical zones (CD-017) and Public presentation (CD-013)
remain separate gaps. No paid or rights-uncertain data is needed for this slice.

## Reusable preview acceptance

Use `scripts/run_account_whatif_preview.py` with a state directory under this
worktree's `output` and port 8032 (never 8000/8031). First verify three COMPLETE
synthetic saved Actions; English demo labels must not enter the stock-symbol
literal parser. Reopen existing state without reapplying the original unheld
SMR watch seed after SMR becomes a held position. Check saved revision hashes
before/after restart and arithmetic. Every READY requires an actual listening
PID, matching full source SHA/digest, HTTP and desktop/mobile browser checks.

`scripts/check_account_whatif_browser.cjs` checks three symbols in both widths,
missing-price unknowns, integer-cent cash/NAV targets, oversell/refusal/recovery,
read-only refresh, history preservation and blocked external/browser mutations.
Human screenshot inspection is also required: no long HHI decimals or fixed
mobile controls obscuring the active form. Exact financial values stay in the
receipt; rounded UI values are expressly approximate.

Use a complete isolated virtual environment, not a `.pth` reference to another
worktree's dependencies: the Windows runtime bootstrap explicitly inserts its
own purelib path and does not process that reference. Run a real child HTTP gate
before the full suite. A test PASS line with cleanup errors or missing subprocess
dependencies is not a completed canonical gate. Preserve failed logs and retry
only the diagnosed boundary.
