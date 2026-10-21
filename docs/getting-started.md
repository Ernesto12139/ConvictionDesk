# Getting started

ConvictionDesk runs in two modes from the same code:

- **Keyless replay** — the default. A read-only, fictional workspace with saved research, for exploring the product in a minute. No keys, no model or market-data calls, no writes.
- **Your own portfolio** — live mode. Your holdings, your provider keys, real company news, prices and filings. Every paid action asks for confirmation first.

Python 3.12 or later is required.

## Keyless replay

```bash
git clone https://github.com/Ernesto12139/ConvictionDesk.git
cd ConvictionDesk
python3 -m venv .venv && source .venv/bin/activate
pip install .
python -m market_agent.serve
```

On Windows (PowerShell):

```powershell
git clone https://github.com/Ernesto12139/ConvictionDesk.git
cd ConvictionDesk
python -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\python.exe -m market_agent.serve
```

With Docker:

```bash
docker build -t convictiondesk .
docker run --rm -p 127.0.0.1:8080:8080 convictiondesk
```

Open **http://127.0.0.1:8080/** and follow the journey:

1. **今日关注 · Today** — pick a ranked event and choose **Fast** or **Deep**.
2. **Decision** — read the Action, its reasons, the strongest counterevidence and what would change the call.
3. **查看依据 · Evidence** — every citation opens the source it points to, with its time and provenance.
4. **研究记录 · Records** — reopen saved revisions and compare rounds.

The replay plays back saved research, so it never runs a new model call. The account What-if and live scans need live mode.

## Use your own portfolio

Live research combines four sources. Configure the ones you need; a missing source makes that part of the answer unavailable, never synthetic.

| Powers | Provider | Variable |
|---|---|---|
| Fast / Deep research, Today screening | OpenAI Responses, `gpt-6.1-sol` (server profiles `sol-fast`, `sol-deep`) | `OPENAI_API_KEY` |
| Company news for Today | Finnhub | `FINNHUB_API_KEY` |
| End-of-day prices | Massive | `MASSIVE_API_KEY` |
| Filings and XBRL | SEC EDGAR | `SEC_USER_AGENT` (your name and contact email) |

Costs, limits and terms for each provider: [Data sources and keys](data-sources.md).

**macOS / Linux**

```bash
export MIA_RUNTIME_MODE=OWNER
export OPENAI_MODEL=gpt-6.1-sol
export OPENAI_API_KEY=...
export FINNHUB_API_KEY=...
export MASSIVE_API_KEY=...
export SEC_USER_AGENT="Your Name you@example.com"
python -m uvicorn market_agent.main:app --host 127.0.0.1 --port 8000
```

**Windows (PowerShell)**

```powershell
$env:MIA_RUNTIME_MODE = "OWNER"
$env:OPENAI_MODEL = "gpt-6.1-sol"
$env:OPENAI_API_KEY = "..."
$env:FINNHUB_API_KEY = "..."
$env:MASSIVE_API_KEY = "..."
$env:SEC_USER_AGENT = "Your Name you@example.com"
.venv\Scripts\python.exe -m uvicorn market_agent.main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000/**:

1. Add holdings under **我的组合 · Portfolio** (CSV import) or build a watch list.
2. Refresh **Today**. The scan shows its cost first; cancel sends nothing.
3. Open an event as **Fast** or **Deep**, or ask your own question. A finished Fast task can continue as Deep in the same record.
4. From a saved Action, open the **account What-if** to see a hypothetical fill's effect on shares, cash, NAV and weights.

Other OpenAI-compatible models can be used through `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. They run a single research path without the Fast / Deep profiles.

## Your keys and data

Keys are read only from the server's environment. `.env` files are not loaded, and keys are never sent to or returned by the browser. Research, holdings and history live in `data/market_agent.db`; set `MIA_DB_PATH` to move it. The live service has **no authentication**, so keep it bound to `127.0.0.1`.

## Troubleshooting

- **Port already in use.** Choose another: `PORT=8091 python -m market_agent.serve`, or `$env:PORT = "8091"` in PowerShell.
- **The replay refuses to start.** Clear `MIA_RUNTIME_MODE=OWNER` from your shell; the replay will not run in live mode.
- **"Dataset version refused".** Point `MIA_DEMO_DIR` at a new, empty folder.
- **Network exposure.** The native server listens on `127.0.0.1`. The Docker image binds `0.0.0.0` inside its container, so publish it to localhost only, as shown above.
- **Today ends as partial.** Provider rate limits can stop a large watch-list scan early; the result lists what it covered. Saved receipts can be screened again in one confirmed batch without fetching news again.

## Run the checks yourself

```bash
pip install ".[test]"
python -m pytest -q
python -m market_agent.serve &        # keyless replay on :8080
python scripts/smoke_public_demo.py --base http://127.0.0.1:8080
```

What these checks cover is described in [Verification](verification.md).
