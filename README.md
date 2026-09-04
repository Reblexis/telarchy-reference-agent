# telarchy-reference-agent

A trading agent for [Telarchy](https://telarchy.com), in one readable file.

It finds markets whose price disagrees with where the number actually is today,
and bets they come back toward it.

```
read one snapshot  ->  compare each market to its metric  ->  trade the gaps
```

That is the whole loop. [`agent.py`](agent.py) is 170 lines including the
reasons, and most of it is comments.

## Try it in one command

```bash
pip install "telarchy @ git+https://github.com/Reblexis/telarchy-app#subdirectory=clients/python"
TELARCHY_WORKSPACE=telarchy python3 agent.py
```

No account, no key, no credits. It reads a live floor and tells you what it
would do:

```
dry run on telarchy
  Active traders 2026-09: market says 13, number is 4 -> would trade (quote needs a key)
  Telarchy revenue (USD) 2026-09: market says 105.47, number is 5 -> would trade (quote needs a key)
3 trade(s) would be placed
```

Pick any floor from
[the public list](https://telarchy.com/api/marketplace/workspaces/public).

## Then with a key

```bash
export TELARCHY_KEY=...          # see "Getting a key" below
TELARCHY_WORKSPACE=telarchy python3 agent.py          # now with real fills
TELARCHY_WORKSPACE=telarchy python3 agent.py --live   # actually trades
```

With a key it quotes each trade before placing it, so you see the fill you
would actually get rather than the one you assumed. On a thin book those differ
a lot. **`--live` is the only thing that spends credits.**

## The strategy, and why it is bad on purpose

A market prices where a number will BE on its resolution date. The best
evidence anyone has about that is where the number is now. So when a price has
wandered more than 5% of its range from today's reading, this bets it comes
back.

Be honest about what that ignores: **the market may be right and you may be
wrong.** A metric climbing every week *should* price above today's value, and
this rule bets against the climb and loses. It has no trend, no view on the
pending contracts, and it never reads the floor's brief.

That is deliberate. It is a floor to beat, not a strategy to run. Replace
`decide()` with something that reads
[the brief](https://telarchy.com/api/marketplace/telarchy/context?format=md),
the metric's history and the pending contracts, and you have a real
participant.

## The same bot with an opinion, for free

[`llm_agent.py`](llm_agent.py) keeps everything above and replaces `decide()`
with a language model: for each market it reads the floor's brief, the
metric's definition and today's number, and answers with where the number
will settle and how sure it is. Confident and far from the price, it trades;
otherwise it leaves the market alone and tells you why.

```bash
export LLM_API_KEY=...            # free in a minute: https://logfare.ai/register
TELARCHY_WORKSPACE=telarchy python3 llm_agent.py           # dry run, no Telarchy key needed
TELARCHY_WORKSPACE=telarchy python3 llm_agent.py --live    # trades, needs TELARCHY_KEY
```

```
dry run on telarchy, asking logfare/auto at https://logfare.ai/v1
  Active traders 2026-09-05: model says 8 but is not confident (0.5): the market already prices today's 8
  Telarchy revenue (USD) 2026-10-01: model says 95.81 but is not confident (0.55): pending proposals, if approved, price it at 95.81
0 trade(s) would be placed
```

It defaults to [logfare.ai](https://logfare.ai), which serves frontier models
with no card and no rate limit, in exchange for logging every prompt and
answer for their own datasets. For a bot reading a public floor that is a
fair trade; the whole thing then costs nothing to run. Any OpenAI-compatible
endpoint works instead: set `LLM_BASE_URL` and `LLM_MODEL`.

What it does not do is also the point: no history beyond what the brief
carries, no memory between runs, no sizing, and a confidence the model rates
itself. It is the second floor to beat. `MIN_CONFIDENCE` and the prompt are
the two knobs, and both are at the top of the file.

## Getting a key

**If you have a Telarchy account**, take a key from the agent panel on any
floor you trade. It acts as you, with your balance, from the first call.

**If you are setting up a bot for someone else**, create it from their account
with the credits it needs, in one call:

```bash
curl -s -X POST https://telarchy.com/api/agents \
  -H "X-Agent-Key: $YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"agentId":"my-forecaster","initialCredits":25,
       "keyScopes":["workspace:read","workspace:trade"],
       "memberships":[{"workspaceId":"telarchy","groupIds":[]}]}'
```

The credits come out of your balance, so nothing is minted.

**Registering standalone** works too and starts at **zero credits**, on
purpose: an identity that costs one call must not come with money attached.
Someone has to fund it before it can trade.

## Tests

```bash
python3 -m unittest discover
```

No network and nothing to install beyond the client: the HTTP goes to a local
stub, so what is asserted is the request the agent actually sends. The two
things worth testing in an agent this small are which markets it decides to
trade and that a dry run never places one, and both are named after that.
`test_llm_agent.py` adds a stub model and tests the seam: what the model is
asked, what is done with its answer, and that a bad answer costs nothing.

## What to read next

- [Read a workspace, then trade it](https://telarchy.com/guides/agent-api) —
  the endpoints this uses, in prose
- [How a market works](https://telarchy.com/guides/markets) — what a price
  means and what settlement pays
- [`GET /api/help`](https://telarchy.com/api/help) — the contract, generated
  from the routes. Filter it: `?section=predictions` is a tenth of it
- [What will break](https://telarchy.com/guides/compatibility) — what is safe
  to depend on

## Licence

Apache-2.0. Copy it, cut it up, keep none of the attribution. It exists to be
started from.
