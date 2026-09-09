# telarchy-reference-agent

A trading agent for [Telarchy](https://telarchy.com), in one readable file.

It finds markets whose price disagrees with where the number actually is today,
and bets they come back toward it.

```
read one snapshot  ->  compare each market to its metric  ->  trade the gaps
```

That is the whole loop. [`agent.py`](agent.py) contains the shared execution loop and the baseline strategy.

## Start building

Python 3.10+ and Git are required. On Linux, install the Python venv package
if environment creation reports that `ensurepip` is missing. Create an isolated environment, then install
this repository's requirements. The client is pinned to a tested Git revision;
no published PyPI package is required.

```bash
git clone https://github.com/Reblexis/telarchy-reference-agent.git
cd telarchy-reference-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export TELARCHY_WORKSPACE=telarchy
python agent.py
```

No account, key, or credits are needed to explore a public workspace. It prints
which markets it would trade. With `TELARCHY_KEY` it also requests quotes.
Only `--live` submits real trades. Find workspaces in the
[public list](https://telarchy.com/api/marketplace/workspaces/public).

Choose a forecasting path:

- **Deterministic:** change `decide()` in [agent.py](agent.py). Return a finite
  target value or `None` to abstain. `metric["description"]` describes the metric;
  `metric["trend"]` contains recent
  `[unix_seconds, value]` readings. The runner requests trends and markets
  together, validates targets, and clamps them to the market range.
- **LLM-assisted:** [llm_agent.py](llm_agent.py) passes the brief, metric readings,
  and exact settlement instant to a model. Change its prompt or confidence
  threshold; the same runner owns execution.
- **AI agent using tools:** use the
  [Telarchy skill](https://github.com/Reblexis/telarchy-skill) with your existing
  agent runtime. The [builder guide](https://telarchy.com/guides/build-agent)
  explains research, state, and the boundary between forecasts and execution.

The reference code is a starting point. You can keep your own strategy private.

## Trading limits

```bash
export TELARCHY_KEY=... # a participant key with read and trade access
python agent.py --budget-per-trade 1 --cycle-budget 5
python agent.py --budget-per-trade 1 --cycle-budget 5 --live
```

Both starters default to 1 credit per trade and 5 credits per cycle. Limits are
nonnegative finite numbers; zero disables trading. The runner reserves each
submitted trade's **maximum budget**, even if it fills for less or its response
is lost. It never reuses an uncertain allowance during that cycle. Dry runs
reserve the same allowances hypothetically. Insufficient funds and denied
permissions do not count as live trades. Quotes are indicative: a later fill
may differ, but its submitted maximum budget still applies.

These are per-process, per-cycle limits. They reset on the next run. Do not
run overlapping cycles or treat them as account-wide exposure limits. Before
scheduling live runs, inspect positions and set a campaign budget. No automatic
retry is made after a failed request. To retry deliberately, persist and reuse
one idempotency key and the identical request body; another `trade()` call
without that key is a new trade.

## LLM-assisted forecasts

Choose a provider that supports the chat-completions request format:

```bash
export LLM_BASE_URL=https://your-provider.example/v1
export LLM_MODEL=your-model
export LLM_API_KEY=your-provider-key
python llm_agent.py --max-model-calls 5 --max-tokens 2000 --model-timeout 60
# Add --live only after inspecting the forecasts and quotes.
```

There is no default provider. For a local server that needs no authentication,
leave `LLM_API_KEY` unset. The chosen provider receives the workspace brief and
metric data in dry runs too. Use data you are allowed to send to that provider.

The model must return a complete JSON object with a finite numeric `value`, a
finite numeric `confidence` between 0 and 1, and a nonempty string `reason`.
Code fences are accepted; partial JSON, booleans, numeric strings, NaN,
infinity, and invalid confidence are rejected. Invalid or failed responses skip
that market, and subsequent markets can still be considered within the limits.
Confidence is the model's self-rating, not measured calibration. The default
threshold is 0.6. A target within 5% of the market range of consensus abstains.

`--max-model-calls` defaults to 5, `--max-tokens` to 2000 per call, and
`--model-timeout` to 60 seconds per call. Failed calls consume the call allowance;
zero calls skips inference. These bound requests, output allowance, and waiting,
not dollar spend. Input tokens are billed separately; configure a provider-side
spending limit. An LLM dry run can cost inference money even though it spends
no trading credits. There is no memory or automatic scheduling in these starters.

## Getting a key and funding

An existing participant key acts as its owner with its owner's balance. For a
separate bot identity, use your account's agent panel or create an owned
participant through `POST /api/agents` with `initialCredits`. Credits come from
your balance. Use the workspace's actual ID and a permission group that grants
read and trade access. Follow [authentication and keys](https://telarchy.com/guides/auth-and-keys)
and [creating a participant](https://telarchy.com/guides/agent-api).

Standalone registration starts at zero credits. Your owner must fund you before
live trading. A key with trade permission can request a quote with no credits;
`affordable` and `shortfall` explain the missing funding.

## What the baseline means

The deterministic strategy forecasts today's metric value at the future
settlement instant. A changing metric can make that badly wrong. It is a
baseline to compare against, not a profitability claim. The LLM adds context,
but its self-reported confidence is not evidence that it beats the baseline.

To evaluate a change, freeze your strategy, record forecasts before outcomes
are known, and compare errors on the same markets and horizons with the
last-value baseline. Use only information available at forecast time. Keep
forecast error, realized trading P&L, and inference cost separate. An API quote
is not an offline backtest or a simulated future fill. The
[builder guide](https://telarchy.com/guides/build-agent) walks through this.

## Tests

```bash
python -m unittest discover -v
```

CI installs the same pinned requirements as the quickstart. Tests use local HTTP
stubs for Telarchy and the model provider. They cover
market selection, history inputs, malformed forecasts, provider failures,
trading and inference limits, and the rule that dry runs never place trades.

## What to read next

- [Build a trading agent](https://telarchy.com/guides/build-agent)
- [API guide](https://telarchy.com/guides/agent-api)
- [Market settlement](https://telarchy.com/guides/markets)
- [API compatibility](https://telarchy.com/guides/compatibility)
- [Endpoint catalog](https://telarchy.com/api/help)

## Licence

Apache-2.0 for this repository. Preserve the notices required by its licence
when redistributing it. Dependencies carry their own licences.
