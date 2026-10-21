# Data sources and keys

Each part of an answer comes from a specific source, and each source is labelled in the evidence drawer. When a provider is missing or fails, the affected facts are reported as unavailable or unknown; ConvictionDesk never fills them with synthetic values.

## Providers

| Source | Used for | Variable | Cost and limits |
|---|---|---|---|
| **OpenAI Responses** — `gpt-6.1-sol` | Today screening, Fast and Deep research | `OPENAI_API_KEY` | Pay-per-token on your own account; model availability depends on your account |
| **[Finnhub](https://finnhub.io/)** | Company news for Today | `FINNHUB_API_KEY` | A free personal-use tier exists; check current limits and terms |
| **[Massive](https://massive.com/pricing)** | End-of-day prices | `MASSIVE_API_KEY` | Free *Stocks Basic* tier: end-of-day data, 5 calls per minute. Closing prices, not real-time quotes |
| **[SEC EDGAR](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)** | 10-K, 10-Q and 8-K filings, XBRL facts | `SEC_USER_AGENT` | Free; requires your name and contact email; automated access is capped at 10 requests per second |
| **OpenAI-compatible model** (optional) | A single research path without Fast / Deep profiles | `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Set by your provider |
| **Longbridge MCP** (optional, read-only) | A timestamped quote reference shown next to the completed close | `MIA_RESEARCH_MCP=readonly`, `LONGBRIDGE_MCP_ACCESS_TOKEN` | Set by your Longbridge account; only allowlisted read tools are callable |

Provider data stays subject to the provider's terms. Use your own keys within those terms.

## What each source can and cannot establish

- **Prices are completed closes.** They anchor portfolio math and context; they never verify a premarket or intraday price you mention in a question, which is kept as your assertion.
- **News is evidence of a report, not of the event.** A news item is cited as "material available in this round" with its publication time. Allegations stay allegations.
- **Filings carry two dates.** A filing's publication date and the financial period it reports are separate fields, and both are checked against the task cutoff.
- **Valuation stays unknown without share data.** Market capitalization and multiples require qualified share-class and corporate-action data; until then they are reported as unknown rather than estimated.

## Cost control

Every Today scan, new research task, follow-up and Deep round shows a confirmation with its scope before anything is sent. Cancelling sends nothing. Fast and Deep run within fixed budgets for sources, context size, output tokens and time; see [Architecture](architecture.md#fast-and-deep).

## Where your data lives

| What | Where |
|---|---|
| Provider keys | The server process environment only. `.env` files are not loaded; keys never reach the browser |
| Holdings, research, history | `data/market_agent.db` (SQLite). Move it with `MIA_DB_PATH` |
| Keyless replay | A packaged fictional dataset, copied to `MIA_DEMO_DIR` and verified by hash before use |

The live server has no authentication. Keep it on `127.0.0.1`.
