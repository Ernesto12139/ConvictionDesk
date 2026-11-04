# Showcase

## Investor-first workbench

The home leads with today's scan state, your holdings in research order and the
research question box. Each status chip links to its detail; full provenance and
runtime records sit below the work. This capture is the read-only replay scope
(synthetic accounts, scripted research).

![Home: status chips, a synthetic replay scan card, holdings in research order and the research question panel](assets/cd-home-1440-light.png)

The Investment Case File after APP Fast and same-task Deep, on a 390-pixel phone.
Both rounds stay readable and both remain `UNDETERMINED`.

<img src="assets/cd-case-390-light.png" alt="Phone view of the APP Investment Case File with the first Fast round and the Deep round" width="320">

## v0.12.0 screens

Screens from ConvictionDesk v0.12.0, captured from the default keyless workbench
(`python -m market_agent.serve`, then **组合研究优先级**) at 1440 and 390 pixels with
every external request blocked. SEC filing metadata and financial facts are real,
frozen public captures (`PUBLIC_SEC`). The four accounts, their cash and marks are
invented (`SYNTHETIC`). The research prose is a fixed script (`SCRIPTED`). The
interface is in Simplified Chinese.

## Where to look first

Six synthetic holdings across four accounts, ranked by committed cost (shares ×
average cost). Source coverage sits beside the rank and does not change it. No
holding has a current quote, so current NAV is `UNKNOWN`.

<img alt="The research priority screen at 1440 pixels. A label strip names PUBLIC_SEC frozen capture, SYNTHETIC accounts and prices, SCRIPTED, NOT_LIVE_MODEL, the data cutoff, no keys, no model calls and no orders. A coverage line counts synthetic marks, SEC metadata, comparable financials and current quotes (0 of 6). A table ranks APP, UNP, AAPL, DOW, NVDA and KR by committed cost with their source coverage." src="assets/convictiondesk-v012-priority-light.png" width="100%">

## One record, two rounds

APP in the Synthetic growth account: Fast locates the filings, then Deep adds
comparable January–June revenue and operating cash flow from the same 10-Q as
round 2. Python `Decimal` derives +55.77% and +34.70%. Revenue outgrew cash flow,
there is no confirmed prior thesis and no current price, so the judgment stays
`UNDETERMINED`; it is not turned into HOLD. Round 1 is kept exactly as saved.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-v012-record-dark.png">
  <img alt="The saved APP research record, round 2, with its research cutoff and bound holdings snapshot. Direction not formed, insufficient evidence, UNDETERMINED. Primary evidence: comparable periods of revenue and operating cash flow from the same filing. Key counterevidence: revenue grew faster than operating cash flow. What would change the judgment: a current quote and a confirmed thesis are missing. The next check is after the next filing." src="assets/convictiondesk-v012-record-light.png" width="100%">
</picture>

## Account What-if

Five more APP shares at a hypothetical USD 200 with a USD 1 fee: cash 1,200 → 199,
shares 30 → 35, account value 8,400 → 8,399. The trade itself only costs the fee.
Seven shares would be refused, because one account cannot spend another account's
cash. Nothing is ordered or saved.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-v012-whatif-dark.png">
  <img alt="Single-account What-if for the Synthetic growth account: shares 30 to 35, cash 1,200 to 199 USD, account value 8,400 to 8,399 USD, position weight 71.43% to 83.34%. The trade itself costs minus 1 USD. The note says the research direction stays UNDETERMINED, no order was placed and nothing was saved." src="assets/convictiondesk-v012-whatif-light.png" width="88%">
</picture>

## On a phone

<img alt="The same journey at 390 pixels: the research priority screen with the synthetic, scripted and no-order labels, and the saved APP record showing UNDETERMINED with the next check." src="assets/convictiondesk-v012-mobile-390.png" width="72%">

## How these images were made

Each screen is a direct headless-Chromium capture of the released v0.12.0 source
(`a251e90`) served on loopback, at 1440 × 900 and 390 × 844, with every request to
another origin aborted (none were made). Images are cropped and scaled, and the
two phone screens are placed side by side; nothing is retouched. On-screen times
use the capturing browser's local time zone; saved receipts keep UTC. Provenance:
[`release/ASSET_PROVENANCE.md`](../release/ASSET_PROVENANCE.md).

Earlier screens remain in [`assets/`](assets/): the v0.10.0 filing-first workbench
(`convictiondesk-v010-*`) and the v0.9 replay (`convictiondesk-journey-*`,
`-records-*`).
