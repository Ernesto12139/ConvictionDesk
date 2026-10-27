# Getting started

## Unified v0.11 workbench

After cloning and installing, start the Console with explicit sample boundaries:

```sh
python -m market_agent.unified_workbench --root ./workbench-state --port 8795
```

Open http://127.0.0.1:8795/. **合成体验** uses public SEC captures, synthetic
holdings and scripted Fast/Deep. Actual execution receipts are available beside
research history. **Owner 接入预检** is an empty, read-only configuration sandbox.
It does not discover or import an existing Owner database.

The optional `market_agent.owner_workbench` launcher requires an explicit local
database, a distinct verified backup and frozen source-qualified inputs. Run the
reviewed source checkout with `python -m pip install -e .` for Windows DPAPI
helpers in `scripts/`; keep private inputs and state outside the repository.
`scripts/prepare_owner_inputs.py --help` describes read-only copy extraction,
including an explicit protected original database path. The launcher binds
loopback, defaults to port8797, and refuses8000 without an exact-source admission
record covering independent QA, CI and backup restore. No Owner data, keys or
private history are packaged. Missing or stale prices remain unknown; What-if
never changes the actual portfolio.

Saved credentials, authentication, source rights, freshness and model budget are
separate checks. Fast/Deep sends require scoped consent and the existing durable
reservation; the launcher enforces an additional USD5 operation ceiling. No live
research-quality claim follows from installation.

## Earlier standalone replay

ConvictionDesk v0.10.0 runs the full research journey on your machine without an
API key: real SEC filing metadata, a synthetic account and thesis, scripted
Fast and Deep research, and a `Decimal` account What-if. You need Python 3.12 or
later.

## Run the workbench

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

Open **http://127.0.0.1:8791/** and follow one UNP filing through:

1. Expand **查看申报前的 Thesis 待核查条件** on a UNP card to see the thesis held
   before the filing. DOW, a watch-list name, has none, and its card says so.
2. Choose **研究这份披露 → 开始研究 → 确认开始研究**. Read the evidence, the
   counterevidence and what would change the call; **查看依据** lists the sources.
3. Choose **继续为 Deep · 保留同一条记录** and confirm. Round 2 joins the same
   record; **历史版本** keeps round 1.
4. In the result's What-if, buy 5 shares at 240 with a 1 USD fee. Under
   **费用、其他持仓计价与资本假设**, give KR a mark of 58 on 2026-10-24 and confirm
   the assumption. NAV moves from 12,980 to 12,979 and cash from 10,000 to 8,799.
   Leave KR blank and NAV stays unknown.

Installing downloads dependencies. Running the workbench makes no model, news or
market-data request; the only outbound traffic is your own browser opening a
filing on sec.gov when you click its link.

### Options

| Option | Default | Use it to |
|---|---|---|
| `--port 8899` | `8791` | Pick another loopback port. Port `8000` is refused on purpose. |
| `--root <path>` | `./.public-ivs` | Keep the synthetic database somewhere else. Use a new folder for a clean start. |
| `--journey` | off | Run the whole journey once without a browser and print the evidence, revision ids and `Decimal` results as JSON. |

Loading the app also creates an empty default store, `./data/market_agent.db`,
in the folder you start it from. The workbench does not use it; delete it after
you stop the server if you like.

### Troubleshooting

- **"Port 8791 already in use".** Start with `--port 8899`, or stop whatever holds 8791.
- **`PUBLIC_IVS_UNMARKED_DATABASE_REFUSED`.** The folder given to `--root` already
  holds a database the workbench did not create. Point `--root` at a new, empty folder.
- **Network exposure.** The workbench listens on `127.0.0.1` only and has no
  login. Do not publish the port.

## Check it yourself

```bash
.venv/bin/python -m market_agent.public_ivs --root /tmp/convictiondesk-journey --journey
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -o addopts='' --basetemp=/tmp/convictiondesk-test-root tests/test_public_ivs_journey.py
```

On Windows use `.venv\Scripts\python.exe` and folders under `$env:TEMP`. Use a new
`--root` and `--basetemp` folder each time. What these checks cover is described
in [Verification](verification.md).

## Other ways to run it

### The earlier read-only replay

`python -m market_agent.serve` starts the v0.9 replay on http://127.0.0.1:8080/:
fictional companies XMPL and XGRD with saved Today items and research records.
It only reads saved results, so it does not run research or the What-if. Docker
builds this replay:

```bash
docker build -t convictiondesk .
docker run --rm -p 127.0.0.1:8080:8080 convictiondesk
```

### Live mode with your own keys (experimental)

The same code can run on your own holdings with real providers. This mode is not
part of the v0.10.0 verified scope and its answer quality has not been benchmarked.

| Powers | Provider | Variable |
|---|---|---|
| Fast / Deep research, Today screening | OpenAI Responses, `gpt-6.1-sol` (server profiles `sol-fast`, `sol-deep`) | `OPENAI_API_KEY` |
| Company news for Today | Finnhub | `FINNHUB_API_KEY` |
| End-of-day prices | Massive | `MASSIVE_API_KEY` |
| Filings and XBRL | SEC EDGAR | `SEC_USER_AGENT` (your name and contact email) |

```bash
export MIA_RUNTIME_MODE=OWNER
export OPENAI_MODEL=gpt-6.1-sol
export OPENAI_API_KEY=...
export FINNHUB_API_KEY=...
export MASSIVE_API_KEY=...
export SEC_USER_AGENT="Your Name you@example.com"
python -m uvicorn market_agent.main:app --host 127.0.0.1 --port 8000
```

In PowerShell set the same variables with `$env:NAME = "value"` and run
`.venv\Scripts\python.exe -m uvicorn market_agent.main:app --host 127.0.0.1 --port 8000`.

Setting a key makes a provider available; it does not send anything by itself.
Each Today scan and research run shows its scope and expected cost and waits for
your confirmation; cancelling sends nothing. Keys are read only from the server's
environment, `.env` files are not loaded, and keys are never returned to the
browser. Research, holdings and history live in `data/market_agent.db` (set
`MIA_DB_PATH` to move it). The service has no login, so keep it on `127.0.0.1`.
Costs, limits and terms per provider: [Data sources and keys](data-sources.md).
