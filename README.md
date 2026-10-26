# ConvictionDesk

### From market evidence to a decision you can revisit.

ConvictionDesk is an open-source research workbench for people who own US stocks.
When a company files with the SEC, it puts the filing next to the thesis you held
before it, researches whether anything changed, keeps every round of that
reasoning on one record, and shows what a trade would do to your account before
you make it.

[**Run it locally**](#get-started) · [See one decision through](#see-one-decision-through) · [How it works](#under-the-hood)

<p>
  <a href="https://github.com/Ernesto12139/ConvictionDesk/releases/tag/v0.10.0"><img alt="Release v0.10.0" src="https://img.shields.io/badge/release-v0.10.0-27745F"></a>
  <a href="https://github.com/Ernesto12139/ConvictionDesk/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Ernesto12139/ConvictionDesk/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-27745F"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-27745F">
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/convictiondesk-hero-v010-dark.png">
  <img alt="One research record in ConvictionDesk v0.10.0. On the left, the receipts behind it: SEC 8-K Item 2.02 for UNP, accession 0000100885-26-000319, accepted 2026-10-22 11:46:52 UTC, a real public filing; the prior thesis stored before it, synthetic; revision 1 Fast with evidence, counterevidence and an UNDETERMINED direction at a 2026-10-24 15:18 UTC cutoff, scripted research; revision 2 Deep on the same task; and an account What-if moving NAV from 12,980 to 12,979 USD with no order placed. On the right, the actual research page: direction not formed, evidence insufficient; primary evidence traceable to the SEC original; key counterevidence; what would change the judgment; and the next check to run." src="docs/assets/convictiondesk-hero-v010-light.png" width="100%">
</picture>

<sub>A real record from the v0.10.0 workbench, framed with the receipts behind it. The interface is in Simplified Chinese.</sub>

## Why ConvictionDesk

A stock chatbot can tell you something about a company. It does not know what
you believed before the news, it rarely shows you what argues against its answer,
and it should not be doing arithmetic on your account. ConvictionDesk is built
around those three gaps.

- **Evidence with an identity.** Each filing arrives with its accession number,
  form, item codes and an acceptance time checked against SEC's own clock, one
  click from the original on sec.gov. Research only sees what existed at its cutoff.
- **A thesis you can come back to.** The thesis on screen is the version you held
  *before* the filing. Research answers with the evidence, the strongest
  counterevidence and what would change the call. Thin evidence comes back as
  `UNDETERMINED`, never as a default HOLD. Fast and Deep are rounds of the same
  record, so nothing is overwritten.
- **Consequences you can check.** The account What-if computes shares, cash, NAV
  and weight before and after in Python `Decimal`. A price it does not have stays
  unknown instead of turning into zero. It previews a trade; it never places one.

| | Asking a stock chatbot | ConvictionDesk |
|---|---|---|
| **Starting point** | Whatever you paste in | A specific filing, with its source and time |
| **Your view** | Forgotten between chats | The thesis you held before the event, then each research round on one record |
| **The answer** | Fluent and confident | Evidence, counterevidence and `UNDETERMINED` when the case is not made |
| **The numbers** | Computed by the model | Computed in `Decimal`; unknowns stay unknown |

## See one decision through

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/convictiondesk-flow-v010-dark.svg">
  <img alt="One decision in five steps. 1 Filing: what the company filed; accession, form, items and acceptance time, checked against SEC's own clock. 2 Thesis: why you own it; only a version stored strictly before the filing is used. 3 Research: does this change it; every claim cites evidence in the task and thin evidence stays UNDETERMINED. 4 Revision: continue, don't overwrite; Fast and Deep are rounds of one task and saved rounds are immutable. 5 What-if: what would it change; shares, cash, NAV and weight in Python Decimal, missing prices stay unknown." src="docs/assets/convictiondesk-flow-v010-light.svg" width="100%">
</picture>

Start the workbench ([two commands](#get-started)) and follow one UNP filing all
the way through:

1. **Notice the filing.** The workbench opens on recent SEC filings for your
   holdings and watch list. Each card says why it is there and what to check
   first. Expand **查看申报前的 Thesis 待核查条件** to see the thesis that was on
   record before the filing. DOW, a watch-list name, has none, and the card says so.
2. **Research it.** Choose **研究这份披露 → 开始研究 → 确认开始研究**. The answer
   cites its evidence, names the strongest counterevidence and says what would
   change the judgment. **查看依据** lists every source with its time and type.
3. **Go deeper without starting over.** **继续为 Deep · 保留同一条记录** adds
   revision 2 to the same task. **历史版本** keeps revision 1 exactly as it was.
4. **Preview the account.** In the result's What-if, buy 5 shares at 240 with a
   1 USD fee. Under **费用、其他持仓计价与资本假设**, give KR a mark of 58 on
   2026-10-24 and confirm the assumption. NAV moves from 12,980 to 12,979 and cash
   from 10,000 to 8,799. Leave KR blank and NAV reads unknown, which is the point.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/convictiondesk-v010-whatif-dark.png">
  <img alt="The account What-if receipt for the synthetic demo account: shares 10 to 15, account cash 10,000 to 8,799 USD, account NAV 12,980 to 12,979 USD, position weight 18.49% to 27.74%; trade itself minus 1 USD; repricing of existing shares not calculated; modeled unrealized P&L 1,399 USD; original research direction UNDETERMINED, no order placed and nothing saved to holdings or research history." src="docs/assets/convictiondesk-v010-whatif-light.png" width="720">
</picture>

**What you are looking at.** The SEC filings for UNP, KR and DOW are real public
metadata captured on 2026-10-24; open any of them on sec.gov. The account,
holdings, thesis and prices are written for the demo and labelled `SYNTHETIC`.
Fast and Deep come from a script, labelled `SCRIPTED`: no model runs and nothing
leaves your machine. The workbench shows these three labels wherever the data appears.

More screens, including the phone layout: [Showcase](docs/showcase.md).

## Get started

You need Python 3.12 or later. Installing downloads dependencies; running the
workbench needs no API key and makes no model or market-data calls.

**macOS / Linux**

```bash
git clone https://github.com/Ernesto12139/ConvictionDesk.git
cd ConvictionDesk
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m market_agent.public_ivs --serve
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/Ernesto12139/ConvictionDesk.git
cd ConvictionDesk
python -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\python.exe -m market_agent.public_ivs --serve
```

Open **http://127.0.0.1:8791/**. Ports, data location, the earlier read-only
replay and troubleshooting are in [Getting started](docs/getting-started.md).

## Under the hood

ConvictionDesk is one Python service (FastAPI, Pydantic v2, stdlib `sqlite3`) with
a dependency-free HTML/CSS/JS console. There is no agent framework, vector store
or queue: the research loop is plain, typed Python, and code has the final say on
what may be saved.

- **Point in time.** Filing acceptance, capture time and the research cutoff are
  separate fields. A source later than the cutoff cannot be cited.
- **Thesis before evidence.** The thesis used is the latest version stored
  strictly before the filing's eligibility boundary. If there is none, the record
  says so rather than inventing one.
- **Validated answers.** Every material claim must cite evidence that exists in
  the task. Answers that invent a number or upgrade an assumption into a fact are
  rejected, not saved.
- **Immutable revisions.** Research is stored as hash-checked revisions; follow-ups
  append a new round.
- **Deterministic finance.** Portfolio and What-if arithmetic runs in `Decimal`.
  Missing prices stay unknown, never zero.

| Concern | Start here |
|---|---|
| The keyless workbench and its data | [`public_ivs.py`](src/market_agent/public_ivs.py) |
| SEC filings, acceptance time and coverage | [`sec_filings.py`](src/market_agent/sec_filings.py) |
| The thesis in force before a filing | [`sec_thesis_delta.py`](src/market_agent/sec_thesis_delta.py) |
| Research loop, validators and revisions | [`research_task.py`](src/market_agent/research_task.py) |
| Typed decision and its checks | [`action_decision.py`](src/market_agent/action_decision.py) |
| Account What-if in `Decimal` | [`portfolio_whatif.py`](src/market_agent/portfolio_whatif.py) |

The longer walkthrough is in [Architecture](docs/architecture.md); what each
capability is tested against is in [Verification](docs/verification.md).

## Built to be trusted

- **What v0.10.0 is.** A complete, keyless run of the journey above on real SEC
  metadata, a synthetic account and scripted research. Live mode with your own
  keys and holdings exists in the code but is experimental and outside this
  release's verified scope; its answer quality has not been benchmarked.
- **It stays on your machine.** The workbench is a single-user local service with
  no login. Keep it on `127.0.0.1`.
- **It never trades.** There is no broker connection, and the What-if writes
  nothing to your holdings.
- **It is a research tool, not investment advice.** It makes no accuracy or
  return claims.

## Open source

ConvictionDesk is [MIT licensed](LICENSE). Issues, bug reports and product
feedback are welcome: see [Contributing](CONTRIBUTING.md).

[Getting started](docs/getting-started.md) · [Showcase](docs/showcase.md) ·
[Architecture](docs/architecture.md) · [Data sources](docs/data-sources.md) ·
[Changelog](CHANGELOG.md) · [v0.10.0 release notes](release/V010_RELEASE_NOTES.md) ·
[Releases](https://github.com/Ernesto12139/ConvictionDesk/releases)

SEC filing metadata and any provider data remain subject to their sources' terms.
