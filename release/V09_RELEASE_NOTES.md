# ConvictionDesk v0.9.0 — limited beta

v0.9.0 is a **limited beta**. Its new capabilities are verified with synthetic or
frozen data only; the quality of live research is not benchmarked. The exact
upstream source and file hashes are in `release/RELEASE_MANIFEST.json`.

## New since v0.8.1

- **Decision-first research page.** It opens on the Action (`ADD` / `HOLD` /
  `REDUCE` / `EXIT`, or `UNDETERMINED`). Up to four distinct reasons, the
  strongest counterevidence, the change condition and the thesis delta are each
  shown once. Repeated citations of one source are disclosed as one source. On
  phones the next check sits under the conclusion. Opening a saved record no
  longer scrolls or moves focus. Diagnostics stay collapsed under *执行记录与诊断*.
- **Single-account What-if** on a saved Action. It defaults to the account
  holding the stock (several holders require a choice) and takes explicit
  shares, a hypothetical fill and fees. It shows cash, shares, NAV, weights and
  concentration, and separates the trade itself, repricing of existing shares
  and modeled P&L in plain `Decimal` values. Missing prices stay unknown. It
  places no orders and changes no saved holdings or research.
- **Synthetic technical conditions.** Regular-session OHLCV is qualified first;
  SMA20, simple ATR14 and 10-session candidate bands follow, with a hypothetical
  account preview. Real provider OHLCV is not admitted.
- **Same-task Fast → Deep continuation** with explicit mode and cost
  confirmation and immutable revisions; historical TTM in Deep's cited context;
  per-symbol Today coverage; local price-scenario receipts from dated prices
  you supply, refusing known share-class or corporate-action conflicts.
- **README screenshots** recaptured from this build on fictional data.
- **CI** now also audits pull-request heads; the browser journey requires the
  next check to be visible at each width and the Action direction and status to
  be shown.

## Verification

Public CI runs the deterministic suite on Ubuntu and Windows (Python 3.12), a
keyless HTTP journey and a Playwright browser journey of the read-only replay,
a non-root read-only Docker smoke and a public-history audit. An independent
engineering review accepted this source (synthetic scope). These checks prove
the software behaves as specified; they are not evidence of investment results.

## Not verified

- Quality of live Today screening, live Fast / Deep research and live Action
  direction. No accuracy, return or backtest claim is made.
- Real provider OHLCV and technical levels; corporate actions under 15% can
  bypass the continuity guard.
- Live valuation (market cap, EV, multiples), extended-provider keys and a
  multi-language interface.

The shared keyless replay is fictional and read-only: it does not run What-if
or technical calculations. ConvictionDesk is a research tool, not investment
advice.
