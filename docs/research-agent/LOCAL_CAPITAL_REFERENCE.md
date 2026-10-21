# Local provider-reported capital reference

The optional personal display now distinguishes a provider's reported market-cap
field from this application's qualified computations. It reads existing private
capture files only. It makes no network requests, writes no provider data and
does not add sources to ResearchTask, Revision, Deep Context or model requests.

This closes the display-only gap where a captured reported field could only be
represented as UNKNOWN because independently computed capitalization was blocked.
It does not qualify a current market cap, derived multiple or investment action.

## Qualification boundaries

| Layer | Product admission |
| --- | --- |
| SOURCE_REPORTED_REFERENCE | Optional local display of the exact `results.market_cap` field, USD unit, raw hash, source URL, observation time and explicit unknown provider effective time. |
| DATED_DERIVED_ESTIMATE | Not admitted by this change. Requires separately established calculation rights and dated price/share/class/action compatibility. |
| FULLY_QUALIFIED_COMPUTATION | Existing frozen financial validator remains unchanged. Its synthetic corporate-action gate is not a live provider entitlement or capital-event proof. |

Massive documents its reported cap as latest close times weighted equivalent
shares. Those shares may span converted share classes. The separately saved
regular close is displayed with its trading date and unadjusted identity, but is
never asserted to be the price used by the provider's cap. Cap price/share dates
and field effective time remain unknown; observation is not effective time.
No price-times-share calculation, adjusted-price ratio, empty-split inference,
EPS splice, P/E, P/S, fair value or capital-action all-clear is introduced.

The same mechanism can display a reported field for a profitable, loss-making,
early-commercialization or multi-class issuer without pretending to reconcile
its equity. Existing computed values stay UNKNOWN when qualification is missing;
qualified negative/zero denominators retain NOT_MEANINGFUL. The frozen source
data, cutoff, original JSON hashes and research conclusions are never amended.

## Private local activation

Use only for the account holder's permitted personal display. Confirm account
and dataset-specific restrictions before enabling; possession of a key alone
does not establish rights. Default mode is disabled, including public demo.

Set server environment `MIA_CAPITAL_REFERENCE_MODE=personal-display` and
`MIA_CAPITAL_REFERENCE_DIR` to a private directory containing the previously
captured `journal.json` and `<SYMBOL>-overview.json` / `<SYMBOL>-close.json`.
The journal binds `symbol`, `endpoint`, exact credential-free URL, parameters,
HTTP status, retrieval time and raw SHA256. No browser path or upload is accepted.
Only today's overview request (empty parameters) and explicitly unadjusted daily
close are supported. Historical `date=` overview queries can backfill filings;
they are rejected rather than treated as historical knowledge.
Display responses additionally require a direct loopback client and localhost
Host, without forwarding headers. External clients, public Hosts and forwarded
requests receive no provider values even when the local server opt-in is set.

Open an existing research record with cutoff at or after both observations.
The valuation API shows a separate `local_capital_reference` property. A capture
after the research cutoff, malformed identity, unknown timezone, changed hash,
unsupported currency or missing class/issuer identity fails closed without values
or private paths. The normal valuation response's frozen basis applies to its
computed metrics; the optional reference explicitly states it is outside the
immutable task. Removing the directory/configuration removes only this overlay.
Snapshots observed more than seven calendar days before the task cutoff are
STALE and expose no value in this overlay. This age ceiling is a display guard,
not proof of current market accuracy. When both layers have values, the page
states that their price/share/class bases are unreconciled and not interchangeable.

## Rights and evidence

Reviewed official sources on 2026-10-18:
[market-data terms](https://massive.com/legal/market-data-terms-of-service),
[overview fields and date semantics](https://massive.com/docs/rest/stocks/tickers/ticker-overview),
[daily close fields](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary).
These support a restricted personal display interpretation, subject to account
and third-party terms. They do not establish derived/non-display, public
redistribution or external-model transfer rights. No new raw-data persistence
right is assumed: only existing captures are read. Original data and private
rights analysis stay outside Git. Releasing generic code does not license data.

Synthetic adapter/API regressions: `tests/test_capital_reference.py`; existing
valuation/IDC/Cockpit validators continue to apply. Same-run real-capture replay,
HTTP/browser evidence and independent review are external handoff artifacts;
test existence alone is not final acceptance. Real-model quality, current
capital-basis computation, live-news qualification and public v0.9 release remain
separate unresolved gates. Owner8000 and public v0.8.1 stay unchanged.

## Bounded Today companion repair

The same sprint repairs independent holding-volume and deadline starvation
counterexamples. Selection reserves holding, fresh-signal/new-arrival, deadline
and nonholding lanes, with a distinct issuer offered before a second slot for
one issuer. Keyword/SEC clues are deterministic candidate priorities only; they
never become verified materiality or cause deletion/suppression without semantic
screening. Old backlog, source cursors, cost confirmation and manual continuation
remain intact. At most four current packets are offered to the existing compact
screen per primary request; this change does not raise cost/token/request limits.

Audit counts report current/offered/deferred candidates and publication-to-offer
delay. The reported reduction means deferral, not proven noise removal. No
approximate cross-outlet event clustering is implemented; reposts can still cost
later slots. At a frozen1196-current snapshot, four per batch implies at least299
confirmed batches to drain if there are no new arrivals or expiries. The lane
repair shortens named critical-clue latency but cannot raise that service rate or
guarantee discovery under arbitrary overload. A separate next gate is conservative
receipt-preserving pre-screen clustering with revision/contradiction protections
and measured token/output budgets, before increasing the event cap.
