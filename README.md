<p align="center">
  <img src="docs/assets/investment-case-hero.svg" alt="ConvictionDesk Investment Case File. Position, saved research case, evidence, then hypothetical account consequence. Conceptual illustration." width="100%">
</p>

**ConvictionDesk is a local investment research workbench for people who own US stocks.**
Start with a position and one account. Compare what you knew at the first pass with
what later evidence adds, then see how a hypothetical change would affect that account.

![The actual desktop Investment Case File showing two saved APP research versions with the same UNDETERMINED status](docs/assets/investment-case-1440.png)

This Console capture uses a synthetic account, historical public SEC filings and
scripted research. [390 px mobile view](docs/assets/investment-case-390.png).

[Run it locally](#run-it-locally) · [Walk the case](#one-position-one-case-file) ·
[Under the hood](#under-the-hood) · [Stable release](https://github.com/Ernesto12139/ConvictionDesk/releases/tag/v0.12.0)

## Why keep a case file?

If you manage positions across accounts, a new filing can add evidence without
changing your investment judgment. The workbench keeps the two versions together
so you can see which assumption, counterargument or next check changed. Before
acting, preview the cash and concentration effect on the account that would
actually fund the change.

The research queue ranks positions by committed cost and shows evidence coverage
separately. Missing quotes leave current NAV unknown.

## One position, one case file

1. **Position.** Open **组合研究优先级**, choose APP and select the synthetic
   Growth account. The account and snapshot date remain visible in the case.
2. **Case.** Save a Fast pass, then continue Deep on the same task. The case file
   shows the two saved judgments, counterevidence and next checks together.
   Both APP versions retain `UNDETERMINED`.
3. **Evidence.** Open the new evidence section to see the comparative revenue
   and operating cash flow periods from the filing. The case also links to the
   evidence reader and the selected revision's execution trace.
4. **Consequence.** Jump to the account What-if. Use five shares, an assumed
   $200 fill, a $1 fee and $240 for the other holding. The synthetic account
   preview moves cash from $1,200 to $199 and shares from 30 to 35. Reopen
   the first saved revision to see its evidence and trace again.

[Full portfolio journey and source details](docs/portfolio-priority-journey.md)

## Run it locally

With Python 3.12 or newer, run these commands from a checkout. Install time
depends on your connection and Python environment.

```sh
python -m pip install .
python -m market_agent.serve
```

Open [the local workbench](http://127.0.0.1:8080/). The interactive journey is
available from **组合研究优先级**. The published v0.12.0 release includes the
prior portfolio journey. This branch adds the Investment Case File.

```sh
docker build -t convictiondesk .
docker run --rm -p 127.0.0.1:8080:8080 convictiondesk
```

## Under the hood

- **Research and task memory.** The existing research service binds the account
  and snapshot, routes the bounded tools and saves immutable revisions. The
  case file reads those revisions directly. [Research service](src/market_agent/research_task.py)
- **Evidence and context.** The portfolio journey uses a bounded filing
  fixture and explicit source dates. A Deep pass can add comparable filing
  facts. Missing facts stay visible in the saved case. [Journey implementation](src/market_agent/portfolio_priority_journey.py)
- **Account consequence.** What-if uses Python Decimal and returns a detached
  receipt tied to the research revision. Account cash and share limits govern
  each scenario. [What-if engine](src/market_agent/portfolio_whatif.py)
- **Trace and evaluation.** The execution receipt records tools, source use
  and the execution mode. The case filters the receipt list by the selected
  revision. [Trace](src/market_agent/research_execution_trace.py) ·
  [Journey eval](evals/portfolio_priority_eval.py)

## Trust and scope

The shipped public journey uses synthetic holdings and marks, frozen public SEC
evidence and a scripted model path. It requires no API key, live quote feed
or broker connection. Research history and hypothetical calculations are
separate from order execution. The default native server listens on loopback.

[Documentation](docs/README.md) · [Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)
