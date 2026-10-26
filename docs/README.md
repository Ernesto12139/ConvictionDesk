# ConvictionDesk documentation

## Start here

| Guide | What is inside |
|---|---|
| [Getting started](getting-started.md) | Run the keyless workbench, its options and troubleshooting, the earlier replay and experimental live mode |
| [Architecture](architecture.md) | The research loop, Fast and Deep, point-in-time evidence, `Decimal` finance, validation, MCP and evaluation |
| [Data sources and keys](data-sources.md) | Each provider, what it can establish, costs, and where your keys and data live |
| [Verification](verification.md) | How far each capability is verified, CI gates and known limits |
| [Showcase](showcase.md) | v0.10.0 screens: filings, a research record, the account What-if and the phone layout |

## Design notes

Deeper notes on individual capabilities, written alongside the code:

| Note | Topic |
|---|---|
| [Account What-if](research-agent/ACCOUNT_WHATIF.md) | Single-account hypothetical fills, repricing versus trade impact, refusals |
| [Today candidate events](research-agent/TODAY_CANDIDATE_EVENTS.md) | The deterministic candidate projection that runs before Today's bounded screen |
| [Dated user valuation](research-agent/DATED_USER_VALUATION.md) | Local price-scenario receipts from your own dated prices |
| [Local capital reference](research-agent/LOCAL_CAPITAL_REFERENCE.md) | A provider-reported market-cap display, kept apart from qualified computations |
| [Technical conditions](research-agent/TECHNICAL_CONDITIONS.md) | OHLCV qualification and candidate price bands |
| [v0.9 product closure](research-agent/V09_PRODUCT_CLOSURE.md) | The end-to-end journey closed in v0.9 |

## Project

- [README](../README.md) — what ConvictionDesk is and why
- [Contributing](../CONTRIBUTING.md) — how to report issues and how releases are made
- [Changelog](../CHANGELOG.md) — what changed in each release
- [v0.10.0 release notes](../release/V010_RELEASE_NOTES.md) · [v0.9.0 release notes](../release/V09_RELEASE_NOTES.md)
