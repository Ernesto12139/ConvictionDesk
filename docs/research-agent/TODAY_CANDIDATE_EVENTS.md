# Today: event candidates before bounded semantic screening

D-076 adds a deterministic, conservative pending-repost projection before the
existing four-unit screen. It is not semantic identity proof or materiality.
Only still-current, unscreened packets are considered; old backlog remains.

Current production ingestion also preserves equal-wording articles from distinct
original sources. A weak title/hash alias is not an original-article identity.
Only a strong unchanged article ID/canonical URL plus unchanged publication and
event metadata may deduplicate a redelivery. Same-article changed metadata/time
is a revision. Previously unretained upstream originals cannot be reconstructed;
the explicit legacy retained-replay path preserves its historical input semantics.

Complete-link matching requires the same symbol/thesis, exact publication
precision within36h, matching numbers/named subjects/disclosure stages/source
event identities, and high normalized headline overlap. Uncertain titles,
different lawsuits/customers/quarters, changed numbers, rumors vs denial vs
confirmation, corrections, explicit new disclosures and revision_of packets
remain separate. No new embeddings, model calls or framework is introduced.
Comparisons cap at20k; additional work stays individual, not discarded. English
headline token matching deliberately favors precision; Chinese-only/low-overlap
reposts may remain separate. Publication proximity is not an event-time claim.

A candidate unit contains every original EvidenceItem and uses its oldest
member's event key. No original attention_events payload/hash is rewritten or
deleted. All member receipts travel to the existing model; the model is told to
inspect each receipt and flag conflicting developments UNKNOWN. Success copies
the shared unit screening result to the covered article rows atomically, with
the exact unit/member mapping in the immutable scan report and selected item's
screening_lineage. Article coverage is not separate model judgments per article.
Failure marks none screened; explicit manual continuation retries the saved
receipts. There is no automatic loop. A later receipt is always considered anew;
earlier screening is not automatically reused to swallow confirmations/revisions.

No persisted schema/model migration is needed. Queued AttentionItem packets,
Evidence Registry and freeze_row carry all sources through the existing Research
Task context. Candidate repost grouping stays distinct from the existing
cited-semantic-group mechanism. The UI shows this distinction and source count.

Limits: at most8 receipts per unit,8k candidate bytes per combined unit and12k
bytes for the selected batch, leaving room under the existing18k compact prompt
guard. Oversized candidates stay pending with explicit capacity disclosure.
Optional related context can still hit the hard18k guard before any model send;
failure remains PARTIAL. Output3072 tokens, four units, one primary request and
manual cost confirmation remain. More receipts may increase tokens within that
bound: fewer semantic units does not promise a cheaper batch. No live provider or
real model quality improvement is claimed from synthetic grouping metrics.

Report fields distinguish raw articles, candidate units, covered articles,
semantic units screened, deferred bytes, backlog/expiry, and comparison limits.
Frozen six-repost tests can show6 articles ->1 unit,5 avoided slots, original
evidence intact. Different events and new disclosures have negative oracles.
Low-recall cautious grouping is preferable to merging unrelated material events;
real-media coverage and held-out reviewer results must be reported separately.
