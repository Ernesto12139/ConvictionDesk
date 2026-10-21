# CD-017: qualified daily technical conditions — D-080

Why this slice: Today cannot obtain a new real recall/precision conclusion from
published reviewer examples, unqualified provider rights or scripted judgments.
Existing news/attention covers bounded scans and explicit partial coverage;
SEC/TTM and capital qualification already refuse unknown basis. Massive's
`completed_history` retains split-adjusted **closes**, not normalized complete
regular-session OHLCV with independent halt/action/usage-rights proof. No provider
was called. A tiny technical core plus visible receipt-to-What-if journey is a
testable next step; it does not solve the live input gap by relabeling fixtures.

Current admission is **SYNTHETIC only**, with owned synthetic usage rights.
REAL / USER_ASSERTION / UNKNOWN / display-only inputs cannot emit numeric zones.
Source ID/note, retrieval and adjustment vintages, regular session, adjustment
basis, no known split/halt, reconciled single common class, exact positive OHLC
and integer positive volume are required. Known saved-task class/action conflicts
also refuse. Every bar must be published/available after its session close and
by the saved research cutoff. At least21 contiguous sessions are needed, ending
at the latest completed session at that cutoff. Missing, duplicate, stale,
incomplete or post-cutoff bars fail closed. No interpolation or split repair.

Calendar: published NYSE 2026–2028 holidays and early closes, New York time,
[NYSE hours/calendar](https://www.nyse.com/trade/hours-calendars), checked
2026-10-20 Sydney. Regular equity close16:00, listed early close13:00; DST is
provided by zoneinfo. Emergency closure/halt coverage is not independently
verified; no live admission follows from this limited calendar.

Algorithm `daily-range-atr-v1` uses Decimal precision64: SMA20; simple mean of
the last14 true ranges (not Wilder smoothing);10-session low/high extremes.
Support is [minimum low, minimum low +0.5ATR]; resistance is
[maximum high -0.5ATR, maximum high]. Bounds round outward to10 decimal places,
not exchange execution ticks. Flat/overlapping ranges refuse. A close outside
the range, the next completed bar, or an action/halt/basis change invalidates the
receipt and calls for re-evaluation, never automatic ADD/REDUCE.

Open a saved Action → technical section → explicitly load owned fictional OHLCV
or paste same-schema synthetic data → compute → inspect source/window/parameters/
cutoff/series hash/receipt → explicitly choose the support upper bound for a
What-if price. The last synthetic close becomes a separately labeled hypothetical
comparison mark. Account/shares/fees and final preview remain explicit user input.
Neither receipt enters Deep or changes saved Action, snapshot or task history.
The public shared keyless replay remains read-only; its screenshots do not show
these new computations. Real input, investment quality, independent QA of this
new candidate, Owner acceptance and public publication remain separate gates.

Worked oracle:21 closes100..120, highs close+2, lows close-2: SMA20=110.5,
ATR14=4, support109..111, resistance120..122. An independent Fraction case with
last high132 gives ATR33/7 and checks outward rounding. Three ticker labels use
the identical engine; no APP production branch. Tests:
`tests/test_technical_zones.py`, `tests/test_portfolio_whatif.py`.

Prior B engineering acceptance is pinned to4deb68d, not transferred to this new
source. B Q2/Q3/Q4 are addressed by decimal input rules, repricing/trade receipt
separation and removal of sticky overlay in What-if. Q1 generic research failure
classification remains a known diagnostic limitation: this slice improves local
calculation errors, does not rewrite old failed tasks or invent their root cause.
Q5 cost-weight fallback is not used by What-if or technical computation; missing
marks still make NAV/weights unknown. See ACCOUNT_WHATIF.md for semantics.


## D-082 continuity admission delta — 2026-10-21

Before any SMA/ATR output, compare each open AND close to the preceding close
using Decimal: abs(current-previous)*100 >= previous*15 => UNAVAILABLE /
PRICE_DISCONTINUITY_REQUIRES_RECONCILIATION. Do not infer a split or rescale bars.
Applies even to NONE_CONFIRMED and SPLIT_ADJUSTED assertions. Existing saved
capital/share-class conflict checks, session/PIT and real-source refusal remain.
The fixed threshold is a conservative review trigger, not calibrated split detection;
legitimate event gaps refuse too; smaller or distributed adjustments may escape.
No provider endpoint, rights claim or corporate-action oracle was added. T1 is
PARTIALLY_MITIGATED, still BLOCKED before REAL admission and same-SHA B verdict.
AAPL/JPM/SMR and 1:10/10:1 plus smaller20% piecewise scales are deterministic
counterexamples in tests/test_technical_zones.py; smooth synthetic baseline remains
available. No additional real corpus request, no model, no numeric repaired output.
