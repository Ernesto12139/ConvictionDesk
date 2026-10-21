# Contributing to ConvictionDesk

Thanks for taking the time to look closely. Bug reports, product feedback and questions are all welcome as [GitHub issues](https://github.com/Ernesto12139/ConvictionDesk/issues).

## How this repository is made

Development happens in a private engineering repository. Each public release is an allowlisted export of one verified upstream commit: the exported files and their hashes are listed in [`release/RELEASE_MANIFEST.json`](release/RELEASE_MANIFEST.json), and CI audits the public history, authorship and release tags. Accepted changes are therefore applied upstream and arrive in the next export, rather than merged directly here.

## Reporting a bug

Please include:

- what you ran (the keyless replay or live mode), your OS and Python version;
- the steps, what you expected and what happened;
- for a research result, the task's round number and cutoff time shown at the top of the record.

Never paste API keys, account numbers or your real holdings into an issue. If the problem only appears with your own data, describe its shape (for example, "two accounts both hold the same stock") instead.

## Suggesting a change

Open an issue describing the problem first. Proposals that keep the core guarantees are the easiest to accept:

- financial arithmetic stays in Python `Decimal`, and the model never introduces numbers;
- missing values stay unknown, never zero;
- every material claim cites evidence available at the task's cutoff;
- saved research revisions are never rewritten.

## Running the checks

```bash
pip install ".[test]"
python -m pytest -q
python -m market_agent.serve &        # keyless replay on :8080
python scripts/smoke_public_demo.py --base http://127.0.0.1:8080
```

See [Verification](docs/verification.md) for what CI covers.
