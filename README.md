# Harnessed

Harnessed is a self-improving agent runtime that scores its own work and only changes itself when the score proves it got better.

---

## Contents

- [What it is](#what-it-is)
- [The thesis in four commands](#the-thesis-in-four-commands)
- [The four pillars](#the-four-pillars)
- [Routing: who handles a message](#routing-who-handles-a-message)
- [Delegation: bring your own coding agent](#delegation-bring-your-own-coding-agent)
- [The dashboard](#the-dashboard)
- [Offline by default](#offline-by-default)
- [Quick start](#quick-start)
- [Learn the four pillars](#learn-the-four-pillars)
- [What needs a key](#what-needs-a-key)
- [Contributing](#contributing)
- [Configuration](#configuration)
- [The CLI](#the-cli)
- [What Harnessed is not](#what-harnessed-is-not)
- [Requirements](#requirements)
- [Licence](#licence)

---

## What it is

Harnessed is a self-improving agent runtime built on four pillars: **Loops, Evals, Memory and
Harness**.

- **The loop is about seventy lines of explicit Python** (`run_loop` in
  `src/harnessed/loop/engine.py`). It has two guardrails: the model stops asking for tools, or an
  iteration ceiling is reached. There is no framework underneath it.
- **Providers sit behind a single adapter layer**, so the loop never learns which vendor it is
  talking to. The OpenAI-compatible wire format is the primary path; the Anthropic dialect is a
  second adapter, not the centre.
- **Everything is measured.** Eval suites are YAML, graders are deterministic, and every run, eval
  result and delegation is written to a ledger in one SQLite file.
- **Self-improvement is narrow and checked.** An eval failure triggers a reflection, the reflection
  writes a candidate procedure, and the full suite plus an unseen holdout is re-run. A promoter
  with no model in it accepts or rejects the candidate by arithmetic, with zero regressions
  allowed. Procedures may only append to the system prompt: never code, never tool schemas, never
  policy. That boundary is what keeps self-improvement from becoming self-modification.
- **Hard work is delegated, not reimplemented.** Through the Agent Client Protocol (ACP), Harnessed
  hands coding tasks to an installed coding-agent CLI. The agent is a registry entry, never
  hardcoded.
- **It runs offline by default.** A fake provider replays a committed, reviewed fixture corpus, and
  a socket guard fails any test that tries to reach the network.
- **One command, one process.** `harnessed dashboard` brings up the runtime, the read gateway and
  the UI on one port, with no second terminal and no Node at runtime.

Harnessed has two jobs: to be a tool its operator uses every day, and to be a working curriculum
for agentic and harness engineering. A loop you can read serves both. Any abstraction that makes
the core harder to read has to pay for itself twice over.

---

## The thesis in four commands

This sequence is the definition of done for the core:

```
harnessed eval run smoke --fresh     baseline score
harnessed improve run --dry-run      the flywheel writes a candidate procedure
harnessed improve promote <id>       the deterministic gate accepts it
harnessed eval run smoke --fresh     the score is strictly higher, zero cases regressed
```

If the second score does not rise with zero regressions, the system is instrumented but not
self-improving.

The repository's own proof (ticket 030) shows the smoke score rising from **0.8 to 1.0 with zero
regressions**, reproduced through a real shell. A companion test (ticket 029) shows the same
sequence **rejecting** a candidate that lacks the needed trait. The honest caveat is part of the
product's own output: that proof runs against `provider=fake`, so it proves the promotion
mechanism, not model quality.

---

## The four pillars

### Loops

- One synchronous loop, provider-ignorant, with an iteration ceiling (`HARNESSED_MAX_ITERATIONS`,
  default 12) and a stop when the model asks for no more tools.
- Every iteration is journaled before the next one starts, so a run can be resumed and replayed.
- A session is one run continued message by message; a follow-up is more journaled iterations of
  the same run, never a separately assembled chat history.

### Evals

- Suites are YAML under `evals/suites/` (`smoke`, `holdout`), with cases scored by deterministic
  graders (regex match, contains-all, tool-called and others).
- The runner is resumable: results are a ledger, so an interrupted eval picks up where it stopped.
- A separate routing suite (`evals/routing/`) scores the router by handler and by exact route.
- An optional judge tier (DeepEval, behind the `eval` extra) can inform a grader's score, never the
  promote or reject decision.

### Memory

Three kinds, all in the same SQLite file:

- **Semantic**: facts, searched with SQLite FTS5 out of the box; embeddings are optional.
- **Episodic**: what past sessions did.
- **Procedural**: the promoted procedures appended to the system prompt.

A retrieval gate decides per message whether memory is needed and of which kind, and retrieved
context rides on a token budget (`HARNESSED_TOKEN_BUDGET`, default 4000).

### Harness

- **The flywheel**: reflect on a failure, write a candidate procedure, re-measure on the suite and
  the holdout, promote only by arithmetic.
- **The event bus**: every step publishes an ordered event, scrubbed for secrets before it is
  written. The events table is what the dashboard replays.
- **Doctor**: `harnessed doctor` checks the checkout rung by rung (offline readiness, provider,
  coding agents, router) and says what to run to fix each one.

---

## Routing: who handles a message

Every message is routed first. A router whose only job is to decide reads the session and the
installed agents' own descriptions, then picks one handler:

- **Groq chat**, for a direct conversational answer;
- **Groq with memory**, when the answer needs the operator's own memory;
- **an installed agent** (for example Claude Code or Grok), for repository work or live research;
- **a human**, when the request is ambiguous, destructive or unsafe.

The router is the OpenRouter Decisions API, pinned to a specific model (`HARNESSED_ROUTER_MODEL`).
Each route has a **confidence floor**. Below its floor the message stops as "not sure enough to
act" and nothing runs; the operator sees the routes and their percentages and decides. If the
router cannot answer at all, the message falls back to Groq chat and says so. Every decision is a
ledger row with its options, its confidence and its cost.

When a chain of agent steps finishes, one synthesis pass writes the reply from the agents' output.
The router never enters eval scoring, reflection or promotion.

---

## Delegation: bring your own coding agent

Agents are declared in `agents.toml`, one entry each. `harnessed agents add <name>` writes a
known-good entry from a template; templates exist for **claude-code, codex, pi, grok, hermes,
deepseek** and a **generic command-line skill**.

An entry carries:

- `kind`: `acp` (speaks the Agent Client Protocol) or `cli` (a script run once,
  non-interactively);
- `use_for` and `not_for`: the plain-language descriptions the router routes on;
- `billing`: `oauth` (a subscription; never proxied, its reported cost shown as an API-equivalent)
  or `openrouter` (runs through a local metering proxy that records each turn's real cost);
- a permission mode, allow and deny lists, timeouts, and optional output globs.

What the runtime guarantees:

- **The child never sees a real credential.** Secret variables are stripped from its environment.
- **Permissions fail closed.** Deny rules win, and nothing auto-approves.
- **Confinement is stated, not implied.** The registry serves how each entry is confined, and the
  dashboard says it in words. A command-line skill on Windows runs in a Job Object that contains
  its processes but does not stop file writes, and the dashboard says "Unconfined" rather than
  pretending otherwise.
- **Every delegation is a ledger row**, settled even when the agent fails in an unexpected way, and
  a task that already succeeded is returned from the ledger instead of being run again.
- **Nothing is pushed, merged or deployed by the harness.**

`harnessed agents test <name>` spawns an entry, completes its handshake and records what came back.

---

## The dashboard

```
harnessed dashboard
```

This serves the dashboard at `http://127.0.0.1:8756/`, on loopback only. The UI is a static export
shipped inside the Python package, so Node is needed only to build it, never to run it.

Six pages:

| Page | What it shows |
|---|---|
| **Overview** | Spend, turns, events, the retrieval gate's split, memory totals, a live diagram of how a run flows, recent activity |
| **Console** | Send a message, watch the route, the steps and the reply arrive live, and continue the conversation |
| **Evals** | Eval runs, a side-by-side compare of any two, and the improvement cycles and promotions |
| **Memory** | A diagram of the memory architecture, plus facts, procedures and episodes |
| **Traces** | Every session and run, with a readable step-by-step replay |
| **Models** | Providers, stored prices, the router and its confidence floors, and every agent with its confinement |

The rules the dashboard keeps:

- **The browser computes nothing.** Every figure is the exact string the gateway served; hover any
  figure to see where it came from and the CLI command that prints the same record.
- **Unknown is never zero.** A value nobody reported reads "unknown" with its reason, never `0` or
  `$0.00`.
- **No endpoint strings, raw event names or JSON bodies on screen.** Output is presented by what it
  is: text as text, an HTML preview in a sandboxed frame, an image as an image.
- **One write door.** The read gateway at `/v1` answers `GET` only; the console's writes go through
  a separate control app, which calls the same code the CLI calls.

---

## Offline by default

Harnessed was first built on a machine with no API keys, and that constraint turned out to be
correct on its own terms: deterministic evals need a deterministic provider.

- The default provider is `fake`. It replays a **committed, hand-reviewed fixture corpus** under
  `tests/fixtures/wire/`, selected by an ordered predicate over the prompt, never by an outcome.
- The router has a fake too, so routing is tested offline.
- A **socket guard** fails any test that opens a connection outside loopback unless the test is
  explicitly marked `live`.
- Live tests (real providers, real agents) are opt-in and skipped by default.

No key is needed to build, test or run the harness end to end. With the fake provider, only
prompts that have a recorded fixture get an answer; anything else fails loudly with the fixture it
expected, which is the point.

---

## Quick start

You need nothing installed but a terminal. Block 1 installs [uv](https://docs.astral.sh/uv/), which
fetches Python 3.12 for you. Pick your system.

<!-- install:windows -->
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

<!-- install:macos -->
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

<!-- install:linux -->
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal so `uv` is on your path. Harnessed keeps its ledger in the folder you run it from, so block 2 makes a folder first. It is the same on every system:

<!-- run -->
```sh
uv tool install --python 3.12 "harnessed-runtime[gateway]"
harnessed --version
mkdir harnessed-demo
cd harnessed-demo
harnessed init --provider fake --no-validate
harnessed doctor --offline
harnessed eval run smoke --fresh --provider fake
harnessed improve run --dry-run --provider fake
harnessed improve promote cand_4820460a275e0bc8 --provider fake
harnessed eval run smoke --fresh --provider fake
harnessed dashboard
```

That runs the four-command thesis on the offline `fake` provider, then serves the dashboard at
`http://127.0.0.1:8756/`. No key is needed.

### Your first real model

Run `harnessed init` and give it a `GROQ_API_KEY`, which is all a chat needs. It checks the key
with one call before writing it. Add an `OPENROUTER_API_KEY` later as the step that turns routing
on; without it every message goes to Groq chat and says so.

### From a checkout

```sh
git clone https://github.com/Vinci-virtuoso/Harnessed-runtime.git
cd Harnessed-runtime
uv sync
uv run harnessed doctor --offline
```

`uv sync` installs the Python dependencies only. `uv run harnessed dashboard` serves the UI from
the static export shipped in the package. `harnessed providers` lists which providers are
configured, by the name of their key variable and never its value.

---

## Learn the four pillars

Each pillar has a walkthrough whose commands run on the offline provider and are checked against
the real output, so they cannot go stale:

- [Loops](src/harnessed/corpus/walkthroughs/loops.md)
- [Evals](src/harnessed/corpus/walkthroughs/evals.md)
- [Memory](src/harnessed/corpus/walkthroughs/memory.md)
- [Harness](src/harnessed/corpus/walkthroughs/harness.md)

An installed copy carries them too: `harnessed learn` lists the pillars, and `harnessed learn
loops` prints one.

---

## What needs a key

- **Offline, no key:** building, testing, the four-command thesis, the walkthroughs and the
  dashboard all run on the `fake` provider.
- **Needs `GROQ_API_KEY`:** a real chat with a live model. `harnessed init` checks the key with one
  call before it writes it.
- **Needs `OPENROUTER_API_KEY`:** routing. Without it every message goes to Groq chat and says so.

---

## Contributing

Work from a checkout. Clone the repository and install the Python dependencies:

```sh
git clone https://github.com/Vinci-virtuoso/Harnessed-runtime.git
cd Harnessed-runtime
uv sync
```

That is the whole Python side.

The UI needs Node.js `>=24.19.0 <25`, the version pinned in `dashboard/package.json`. Install,
build and stage the static export that ships in the package:

```sh
npm --prefix dashboard ci
npm --prefix dashboard run build
npm --prefix dashboard run export
```

---

## Configuration

Settings come from the environment or from a `.env` file in the working directory. The documented
variables are in `.env.example`:

| Variable | Purpose |
|---|---|
| `HARNESSED_PROVIDER` | `fake` (default), `groq`, `deepseek`, `openrouter`, `openai`, `ollama` or `anthropic` |
| `HARNESSED_MODEL` | Override the provider's default model id |
| `HARNESSED_SMALL_MODEL` | The cheap model used by the retrieval gate, the reflector and consolidation |
| `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Set only the one you use |
| `HARNESSED_BASE_URL` | Any OpenAI-compatible server (vLLM, Ollama, LM Studio) |
| `HARNESSED_EMBED_BASE_URL`, `HARNESSED_EMBED_MODEL` | Optional embeddings for semantic memory |
| `HARNESSED_ROUTER_MODEL` | The pinned router model |
| `HARNESSED_TOKEN_BUDGET` | Tokens of retrieved memory per request (default 4000) |
| `HARNESSED_MAX_ITERATIONS` | Passes one run may take before it stops (default 12) |

State lives in `.harnessed/`: one SQLite file, `harness.db`, plus agent transcripts. `.env` is
never committed.

---

## The CLI

Every command is a thin layer over one subsystem call, and every read has a `--json` form that
prints the same bytes the gateway serves.

| Command | What it does |
|---|---|
| `init` | Configure a provider, check the key, write `.env`, and say what now works |
| `run` | Run one prompt through the loop and print the reply |
| `eval` | Score an eval suite against the configured provider |
| `gate` | Score a suite and exit non-zero unless every case passed |
| `improve` | Measure, reflect, measure again, and decide by arithmetic |
| `memory` | Inspect semantic, episodic and procedural memory, and write down a fact |
| `agents` | Add, test and list the coding agents in `agents.toml` |
| `decisions` | A session's routing decisions, in order |
| `router` | The router model's latest live probe, and the confidence floors it routes on |
| `sessions`, `runs`, `events` | What the ledgers hold |
| `spend` | What the run journal cost, with the turns it could not price counted beside it |
| `prices` | What each provider charges, read once from its own catalogue and stored |
| `providers` | Which providers are configured |
| `outputs` | A delegation's stored output files |
| `doctor` | Check that this checkout can run offline and deterministically |
| `serve` | The read-only gateway on its own |
| `dashboard` | The UI, the read gateway and the control app on one port |

---

## What Harnessed is not

- **Not a framework.** No plugin system, no DSL, no orchestration layer to learn before you can
  read the loop.
- **Not a hosted service.** Single-operator and local-first: one SQLite file, no accounts, no
  billing, no cloud control plane.
- **Not a chat product.** The console is a front door to a measured run; every reply shows the
  session, the steps and the cost of the run that produced it.
- **Not tied to a vendor.** No provider's wire shape is the internal representation, and no
  coding-agent CLI is hardcoded.
- **Not self-modifying.** Procedures append to the system prompt and nothing else, and the
  promotion gate never asks a model.
- **Not unattended on production.** Live provider calls, destructive repository work and anything
  touching production stay behind an explicit human gate.

---

## Requirements

- **Python 3.12** and [uv](https://docs.astral.sh/uv/).
- **Node.js**, only to build or test the dashboard; the shipped package runs without it.
- **Optional:** a provider key for live runs, and a coding-agent CLI (for example Claude Code) for
  delegation.

---

## Licence

Harnessed is licensed under the [Apache License, Version 2.0](LICENSE) (SPDX: `Apache-2.0`). The
[NOTICE](NOTICE) file carries the attribution.

The PyPI distribution is `harnessed-runtime`; the command and Python import remain `harnessed`.
