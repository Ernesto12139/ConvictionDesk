# Dated user-price reference valuation

D-076 adds an optional entry after the decision/reasons/counterevidence in the
existing financial section: 使用指定日期价格评估. The user chooses a hypothetical
scenario or an owned observation, price as an exact decimal string, USD, New York
date, unadjusted regular/close observation basis and a source note. Observations
remain USER_ASSERTION. Date-only observations must precede the research's NY date;
published weekend/full-closure guards use the [official NYSE2026–2028 calendar](https://www.nyse.com/trade/hours-calendars)
(reviewed2026-10-19 Sydney). Outside that coverage, observations are refused;
explicit scenarios remain possible. Emergency halts, early-close clock time,
actual trading and quote accuracy are not independently verified.

The user must attest local derivation rights. This is not an independent license
check. Third-party source input is not admitted by this route; known Massive/
Polygon display-only copies are rejected even if described as owned observations.
Do not copy a restricted Provider number into this form to avoid its terms.
Hypothetical inputs must actually be user-defined, not relabelled quotations.

The existing frozen `valuation_report` supplies eligible instant common shares
and exact compatible quarters. Share concept must be CommonStockSharesOutstanding
or EntityCommonStockSharesOutstanding, entity-wide scope, with accession, source
hash and effective date. No weighted-average diluted share count is substituted.
Share date must precede price by at most100 days for conditional reference;
D-077 permits101–120 days only as a labelled old-share illustrative scenario,
still constrained by the original120-day task-cutoff selector. Older inputs
remain unavailable. Filing must be public by the
price date/research cutoff (same-day observation/filing ambiguity is rejected).
TTM end must precede price by at most180 days and its availability must precede
the price date. Missing/gapped/restated-incompatible periods stay UNKNOWN.

New SEC summaries retain issuer tickers/CIK/source/observation time from the
already-requested SEC ticker map, with no extra request. Multiple mapped tickers
are conservative conflict evidence, not proof of class equivalence. A snapshot
observed after the research cutoff is not used as historical identity proof.
Legacy summaries without it are downgraded to ILLUSTRATIVE_SCENARIO, even if
users accept every capital assumption. A positive single-listing identity must
be hash-bound and known before the observed New York price day; same-day or
later identity cannot establish historical alignment. It still does not prove
all common shares have equal economic rights.
A shares-only summary also uses its share filing availability, so an otherwise
valid early-company share disclosure does not fail on an empty financial list.

All values in this path remain CONDITIONAL_REFERENCE or ILLUSTRATIVE_SCENARIO,
never current or independently verified capitalization. Users must explicitly
accept unchanged capital basis (including splits/reverse splits, stock dividends,
issuance/buybacks/conversions) and equivalent economic pricing of all reported
common shares. Known source-backed action/class conflicts or multiple listed
common classes are refused; checkboxes cannot override them. Unknown continuity
is not inferred from empty split lists or adjusted-price ratios.

Python Decimal calculates input price × dated SEC common shares. Reference P/S
divides by whole-company historical revenue, without debt/cash/preferred-claim
adjustments. Reference equity value / TTM net income is offered only if the user
explicitly assumes consolidated income is entirely attributable to common equity
(no preferred/NCI deductions). This assumption is prominent and bound to the
receipt; it is not Provider-standard P/E. Nonpositive eligible denominators are
NOT_MEANINGFUL. No EV/DCF/target/forward earnings/return or automatic ADD follows.

`POST /api/research/tasks/{id}/dated-valuation` is an explicitly submitted local,
non-persisting arithmetic evaluation, not research generation. It pins `revision`,
reads hash-checked saved history and returns a price EvidenceItem (USER_ASSERTION)
and a Python calculation receipt. The hash binds task projection, input, source
IDs, price/share/filing/research/evaluation times, exact values, assumptions and
calculation version. Received/evaluated time is separate from historical source
times. These receipts are detached WHAT_IF context, not added to original facts,
Evidence Registry, Task/Revision, Action, Deep or model context. Navigation does
not save them. Future prices are rejected rather than borrowing future knowledge
into a historical research version; use a new analysis context instead.

`PriceEvidence` is the minimum future adapter output: provider/instrument,
price/currency/session/effective date/retrieved time/adjustment/usage rights/
source ID/trust. DISPLAY_ONLY and UNKNOWN rights fail derivation admission.
Only server code can produce SERVER_VERIFIED_DERIVATION; the HTTP schema rejects
client-provided provider/trust/rights promotion. No new live adapter, SDK, model
transmission or provider subscription is enabled. Synthetic and user inputs
retain their source identity. Existing Massive reported-cap display remains
separate and never supplies this computation.

D-077 accepts an optional `comparison_price`, always a self-defined hypothetical
input rather than a fetched quote. Python computes exact same-date/same-shares/
same-TTM differences and approximate display ratios. The full calculation hash
binds both prices. Existing financial, income-attribution and rights guards apply
to both; invalid comparison prices cannot bypass them. The page links back to
the saved thesis delta, strongest counterevidence and action change conditions.
Changing a form input clears the prior result until explicit recalculation.
No original Action, portfolio, revision or model context changes.

Verification is scoped offline arithmetic/API/history/cost/source and isolated
desktop/mobile browser behavior. It does not establish live market accuracy,
investment quality, Owner acceptance or public release eligibility.
