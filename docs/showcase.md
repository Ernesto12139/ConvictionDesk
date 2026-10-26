# Showcase

Screens from ConvictionDesk v0.10.0, captured from the keyless workbench
(`python -m market_agent.public_ivs --serve`) at 1440 and 390 pixels. SEC filing
metadata is real; the account, thesis and prices are synthetic; research text is
scripted. The interface is in Simplified Chinese.

## The filings that matter to you

The workbench opens on recent SEC filings for the demo holdings (UNP, KR) and
watch list (DOW). The banner names the three kinds of data on the page. Each card
gives the form, the SEC acceptance time, why the filing is here and what to check
first, with a link to the original and the thesis held before it.

<img alt="The v0.10.0 workbench first screen at 1440 pixels. A banner reads: public demo, SEC filing to research to account preview, with labels for real SEC metadata, synthetic holdings and thesis, and scripted research. Below it, the company filings lane: a UNP quarterly report card marked synthetic holding, pending review, with its SEC acceptance time in UTC, why it is listed, the next check, the prior thesis link, and buttons to open the SEC original, research the filing and run an account What-if." src="assets/convictiondesk-v010-home-1440.png" width="100%">

## A research record

Researching the UNP 8-K returns the direction (here not formed, because the
filing metadata alone does not establish an operating impact), the primary
evidence, the strongest counterevidence, what would change the judgment and the
next check. The record keeps its cutoff and round number, and Deep continues it as
round 2.

<img alt="A saved research record at 1440 pixels: UNP investment logic review, round 1, research cutoff 2026-10-24 15:18 UTC. Direction not formed, evidence insufficient, UNDETERMINED. Primary evidence traceable to the SEC original, key counterevidence, a note that no comparable before-and-after thesis was saved, what evidence would change the judgment, and a side panel with the next priority check and a follow-up form. The price reference is labelled as a synthetic mark." src="assets/convictiondesk-v010-record-1440.png" width="100%">

## Account What-if

From the result, preview a hypothetical trade. Shares, cash, NAV and weight are
computed before and after in `Decimal`; the trade itself and repricing of shares
already held are reported separately. Nothing is ordered or saved.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-v010-whatif-dark.png">
  <img alt="What-if receipt: shares 10 to 15, cash 10,000 to 8,799 USD, NAV 12,980 to 12,979 USD, weight 18.49% to 27.74%, trade itself minus 1 USD, modeled unrealized P&L 1,399 USD, original direction UNDETERMINED, no order placed." src="assets/convictiondesk-v010-whatif-light.png" width="720">
</picture>

## On a phone

<img alt="The v0.10.0 first screen at 390 pixels: a compact banner with the three data labels, then the SEC filings lane with the first UNP card visible." src="assets/convictiondesk-beta-mobile-390.png" width="390">

## How these images were made

Each screen is a direct headless-Chromium capture of the v0.10.0 workbench; the
What-if is cropped, nothing is retouched, and files carry no image metadata. The
README hero frames one of these captures with an original receipt rail, and the
flow diagram is an original SVG; both are generated from
[`assets/src/`](assets/src/). Provenance:
[`release/ASSET_PROVENANCE.md`](../release/ASSET_PROVENANCE.md).
