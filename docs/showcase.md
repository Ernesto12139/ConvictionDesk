# Showcase

Real screens from ConvictionDesk v0.9.0. Today, Decision, Evidence and Records come from the keyless replay that ships with the repository. The What-if comes from a local server over a copy of the same fictional data, because the shared replay is read-only. Companies, filings, prices and accounts are fictional, and times are shown in UTC. The interface is in Simplified Chinese.

## The journey

Today ranks what changed; a decision answers with an Action; Evidence shows the sources behind it.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-journey-dark.png">
    <img alt="Three screenshots of the keyless replay. 1 Today: two fictional companies, XMPL and XGRD, each with a headline, why it matters, the next thing to verify, and Fast or Deep buttons. 2 Decision: a saved XMPL research record reading HOLD, with the reason, linked evidence and the thesis change compared with the Fast pass. 3 Evidence: the sources behind the decision, numbered and labelled as user premise, historical background or material available in this round." src="assets/convictiondesk-journey-light.png" width="100%">
  </picture>
</p>

## Decision

The first screen answers in one place: direction and status, up to four distinct reasons, the strongest counterevidence, what would change the call and the thesis change. When several points rest on the same source, the page says so instead of counting it twice.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-decision-dark.png">
    <img alt="A saved Deep research record for fictional company XMPL. Header with the question, round number, research cutoff in UTC and a note that this historical revision is read-only. The decision reads HOLD with the reason and evidence links, followed by why, and the thesis change versus the Fast pass. A side panel shows the next priority check." src="assets/convictiondesk-decision-light.png" width="100%">
  </picture>
</p>

## Account What-if

Pick the account that holds the stock, enter a hypothetical fill, and see shares, cash, NAV and weight before and after. The trade itself, repricing of shares you already own, and modeled profit and loss are reported separately, in exact `Decimal`. Nothing is ordered or saved, and the original Action is unchanged.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-whatif-dark.png">
    <img alt="The single-account What-if on fictional XMPL. The synthetic account holds 40 shares and 30,000 USD cash. Buying 10 shares at a hypothetical 61.40 with a 1 USD fee shows shares 40 to 50, cash 30,000 to 29,385, NAV 32,456 to 32,455 and weight 7.57% to 9.46%; trade itself minus 1 USD, repricing of the existing 40 shares against a 58 reference plus 136 USD, total plus 135 USD, modeled unrealized P&L 375 USD; the original HOLD is unchanged." src="assets/convictiondesk-whatif-light.png" width="100%">
  </picture>
</p>

## Records

Every result is an immutable revision. Reopen one when the next event arrives, compare rounds, and see exactly what was known at the cutoff.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/convictiondesk-records-dark.png">
    <img alt="The research workspace home in the keyless replay. Below the Today items, a section titled Continue your last research lists three saved records for fictional companies XGRD and XMPL, each marked research complete and saved, with round number and cutoff time." src="assets/convictiondesk-records-light.png" width="100%">
  </picture>
</p>

## On a phone

<p align="center"><img alt="Four 390-pixel-wide screenshots of the keyless replay: Today in light theme, the first screen of the XMPL decision (action, next check and first reasons) in light and dark themes, and the evidence list in dark theme." src="assets/convictiondesk-mobile.png" width="100%"></p>

## How these images were made

Screens were captured from the release build with a headless browser, then arranged and scaled without pixel edits, and saved without image metadata. Provenance: [`release/ASSET_PROVENANCE.md`](../release/ASSET_PROVENANCE.md).
