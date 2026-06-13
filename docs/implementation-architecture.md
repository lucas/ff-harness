# Implementation Architecture

How we will build the harness. The spec/design context lives in `harness.md` (do not edit — it's the document we follow); the challenge brief and requirements live in `mission.md`. This file tracks implementation decisions only.

**Design priorities (in order):** durability (state survives crashes), resilience (keeps working despite failures), simplicity. Language: **Python**. The harness is the focus; the agent/worker is a thin pluggable thing at the edge.

## Decision status
- **Settled:** Python; durability/resilience-first; simple/single-process; staged-pipeline execution; two-tier SQLite storage; swappable worker behind a minimal protocol; declared per-domain bundle (guardrails + tools + skills + checkpoints); loop-approval gate at 10 iterations; tool registry + dispatch choke point; skill registry; OpenRouter LLM client (configurable model, default free Nemotron, $1/day spend cap); context compaction; sub-agents with isolated sessions; full event + cost logging.
- **Open / deprioritized:** the agent's domain and the real demo input (harness-first for now); deploy target (must support a persistent volume — see below).

## Core stance
The harness is a **durable, resumable run orchestrator**. It drives a worker through a sequence of stages, wraps every step in the four pillars, and persists enough state that any run can be **resumed after a crash** and **replayed from any checkpoint**. The worker is untrusted and interchangeable.

> The run is a durable state machine; the worker is a function the state machine calls.

The orchestrator, store, and four pillars are **domain-agnostic core**. What makes a given agent is a **declared per-domain bundle** — its guardrails, tools, skills, and checkpoints. Swapping that bundle (and the worker) yields a new agent with no changes to the core. The three configurable layers are distinct by intent: **guardrails constrain** (enforced), **tools empower** (callable), **skills guide** (advisory context).

## Two-tier storage (control plane + data plane)
A small core DB indexes users and sessions; each session's run context lives in its own DB so the core never bloats and a session is a portable, isolated unit.

**Core DB — `harness.db`** (control plane):
- `users` — id, identity, created_at.
- `sessions` — session_id (uuid, PK), user_id → users, parent_session_id → sessions (nullable; null = root, set for sub-agents), status (`initializing`/`active`/`awaiting_human`/`completed`/`failed`/`aborted`), current_stage, worker_id, last_checkpoint, created_at, updated_at.
- `spend_log` — ts, session_id, model, tokens_in, tokens_out, cost_usd. Cross-session ledger that powers the global daily spend cap (summing per-session DBs would mean scanning every file).

The per-session path is **derived** from the uuid (`sessions/{uuid}.db`); the canonical pointer is `session_id`. An optional `db_path` override column allows relocation/archival.

**Per-session DB — `sessions/{uuid}.db`** (data plane, self-contained):
- `events` — append-only log (seq, ts, type, stage, payload). The source of truth.
- `checkpoints` — stage, name, status, criteria_results, material_ref, created_at. The replay anchors.
- `material` — typed, validated I/O artifacts (direction, stage, type, content).
- `alarms` — type, severity, context, recommended_action, stage, resolved.
- `run_meta` — single-row snapshot of declared guardrails/limits + worker identity at run start (reproducible replay).

SQLite via stdlib, WAL mode. Postgres only if multi-node is ever needed (not now).

## Durability model (priority #1)
- **Append-only event log is the source of truth.** State is a fold over `events`, not a mutable blob; write-ahead for anything with side effects.
- **Checkpoint snapshots** persist the result plus the material that produced it, keyed by (session, stage) — enables replay from any checkpoint without re-running prior stages.
- **Resume = replay the session log, restart from the last good checkpoint.** A killed process loses nothing.
- **Crash recovery**: the core index lists `active`/`awaiting_human` sessions; reopen each session file and resume.
- **Blast-radius isolation**: a torn/corrupt session file fails that run only; the core index and other sessions are untouched.
- **Idempotency**: stages keyed so re-execution after a crash does not double-apply effects.

## Resilience model (priority #2)
- **Bounded execution** — declared turn cap, token budget, wall-clock timeout, spend ceiling. A confused agent cannot run away.
- **Retries with backoff** on transient failures, capped by declared max-attempts.
- **Fail-as-data** — worker/tool errors become structured results the loop reacts to, never uncaught crashes.
- **Per-stage / per-tool timeouts.**
- **Circuit-break → escalate** — repeated checkpoint failures or a critical alarm halt the run and hand off to a human rather than guessing (HITL).
- **Crash recovery** — because state is durable, recovery is just resume.

## Loop control & human approval
The loop guardrail is **declared**: at most **10 iterations without human approval**. This is a soft gate, not a hard abort. On breach the orchestrator:
1. Persists state and sets the session to `awaiting_human`.
2. Raises the `iteration_limit_reached` alarm (severity: warning; context: iteration count, stage, last checkpoint; recommended action: review progress, then approve continuation or abort).
3. Waits. Because state is durable, an `awaiting_human` run can sit indefinitely and resume exactly where it paused.

On **approve**: append an `approval_granted` event (who/when), reset the iteration counter (grant another 10), flip to `active`, resume from the last checkpoint. On **deny**: set `aborted` and raise a closing alarm. The same approval machinery serves per-tool approval (risky tools flagged `requires_approval`) — two triggers, one HITL path. An optional absolute hard cap can bound total iterations even across approvals.

## Session lifecycle (creation must be crash-safe)
Insert core `sessions` row as `initializing` → create + schema-init the session file (write `run_meta`) → flip core status to `active`. On startup, **reconcile**: `initializing` rows with no valid session DB are swept to `failed`; orphan session files with no core row are swept. The core index stays authoritative for what exists.

## Four pillars as distinct components
Pillar definitions and the PRD-vs-deck mapping are in `harness.md`. Implementation surface:

| Pillar | Component | Serves |
|---|---|---|
| Guardrails | `guardrails.py` — registry of declared policies (caps, tool allow-list, input/output validation) checked at fixed interception points; returns allow/deny/modify | Resilience |
| Checkpoints | `checkpoints.py` — named gates with explicit pass/fail criteria → persisted result | Durability + resilience |
| Material handling | `material.py` — typed input/output ports; validate on ingress/egress; serialize to the session store | Durability + resilience |
| Alarms | `alarms.py` — named types, severity, context, recommended action; routed to handlers (log / escalate / halt) | Resilience |

## Swappable worker boundary
A minimal protocol — `Worker.act(context) -> result` — is the entire surface the harness knows. Any agent (Claude, another model, a mock) implements it; dropping one in requires no harness changes (the swappable-agent Should and the portability Bonus).

## Tools (capabilities the agent can call)
A declared **tool registry**. Each tool: `name`, `description` (what the model reads to decide), typed `parameter schema`, `executor` (the real function), and a `result contract` (parseable output; errors returned as structured data, never crashes). Per-tool metadata: allow-list membership, `requires_approval`, timeout, max_retries, idempotent.

All calls pass through one **dispatch choke point** that: rejects non-allow-listed tools (guardrail), validates args (material handling), enforces timeout + retries/backoff (resilience), returns results/errors as data, traces the call, and raises alarms on failure. `tools.py` holds the registry + dispatch; individual executors can grow into a `tools/` package.

## Skills (guidance that shapes the agent)
A declared **skill registry** of advisory guidance — *not* enforced like guardrails. Each skill: `name`, `description` (one-line, used to select relevance), and a `body` (the procedure/guidance). Skills are stored as markdown with frontmatter and loaded by `skills.py`; the material-handling layer injects relevant skills into the worker's context when assembling a stage.

The contrast that keeps the configurable layers clean: **guardrails constrain** (enforced), **tools empower** (callable), **skills guide** (advisory context).

## LLM provider & cost control
LLM calls go through OpenRouter (OpenAI-compatible API, base `https://openrouter.ai/api/v1`). The API key lives in `.env` (`OPENROUTER_API_KEY`), loaded from the environment — never logged, never committed.

- **Model is configurable.** Default: `nvidia/nemotron-3-ultra-550b-a55b:free` (free tier — chosen because the key currently caps at $1/day). Model id, base URL, and per-model context window live in `config.py`, swappable without code changes.
- **Spend ceiling is a declared guardrail.** Default cap **$1/day**, configurable. Before each call the guardrail sums today's `spend_log` (core DB) and blocks if the cap would be exceeded → `spend_cap_reached` alarm → escalate. Free models report $0 but the path is always enforced.
- Free tier also rate-limits requests; 429s are handled as transient (retry/backoff), and a sustained block escalates.
- `llm.py` is provider-agnostic; OpenRouter is one implementation. Each response yields tokens in/out, cost, latency, and model id — captured for logging and the spend ledger.

## Context management & compaction
To continue past a model's hard context limit, `context.py` manages the worker's working context and **compacts** when it nears a configurable threshold (fraction of the model's window).

- On breach: summarize older turns/tool-results into a compact summary that preserves the task, decisions, current state, open items, and last checkpoint; replace the verbose history with it. The summarization is itself an LLM call (cheapest configured model).
- **Compaction never loses data.** The full record stays in the durable `events` log (source of truth); only the *working context* shrinks. The compaction summary is persisted as an event, so replay stays deterministic.
- A `context_compacted` event logs before/after token counts; repeated compaction with no progress raises an alarm.

## Sub-agents (nested sessions)
A sub-agent is the **same harness loop running in its own session** (`sessions/{uuid}.db`) with fresh context, spawned by a parent worker via a `spawn_subagent` tool. Only the sub-agent's final result re-enters the parent context — its internal turns stay in its own session, so the parent's context is preserved (complements compaction).

- Lineage: the child `sessions` row sets `parent_session_id`; the parent logs `subagent_spawned` (child id) and `subagent_result` events. The session tree is fully durable and replayable.
- Resilience: a sub-agent failure returns to the parent as structured data (fail-as-data); the parent decides. Recursion depth is bounded by a declared `max_subagent_depth` guardrail; the daily spend cap is global across the whole session tree.
- `subagent.py` owns spawn/await; each sub-agent can carry its own skills/tools/worker.

## Logging & observability
The `events` log is also the audit trail — we log thoroughly, structured, per session:

- Each worker turn: assembled input context and raw output.
- Each LLM call: model, tokens in/out, **cost**, latency.
- Each tool call: name, args, result or error, duration.
- Each user/agent input, guardrail decision, checkpoint result, alarm, approval, compaction, and sub-agent spawn/result.

Cost is recorded per call in `events` (full detail) and mirrored to the core `spend_log` (lightweight, cross-session) so the daily spend guardrail is a single query. Secrets are redacted; logging captures full payloads (durability) even though the live context is compacted.

**Named alarm types so far:** `iteration_limit_reached`, `spend_cap_reached`, `tool_failed`, `checkpoint_failed_repeatedly`, `max_subagent_depth_exceeded`, `subagent_failed`, `compaction_stalled` — each with severity, context, and recommended action.

## Module layout
```
harness/
  orchestrator.py   # the engine: resume run, drive stages, bounded loop
  store/
    core.py         # users + sessions index (control plane)
    session.py      # per-session DB: events, checkpoints, material, alarms (data plane)
  guardrails.py     # declared policies + interception points
  checkpoints.py    # gate definitions, evaluation, results
  material.py       # typed I/O ports, validation
  alarms.py         # alarm types, severity, recommended actions, routing
  tools.py          # tool registry + dispatch choke point (allow-listed, validated)
  skills.py         # skill registry + relevance loading (advisory guidance)
  llm.py            # provider-agnostic LLM client; OpenRouter impl; returns usage + cost
  context.py        # context-window manager + compaction (continue past the hard limit)
  subagent.py       # spawn/await nested sessions (own DB, fresh context)
  worker.py         # the Worker protocol + default LLM worker (uses llm.py)
  config.py         # declarative limits, model/provider, caps, tool/skill declarations
```

**Engine in one breath:** load-or-resume run → for each stage from the resume point → guardrails pre-check → run stage with retries/timeout/fail-as-data (compact context near the limit; pause for human approval every 10 iterations) → guardrails post-check → checkpoint evaluate → persist result → on fail: alarm + (retry | escalate | halt); on pass: advance.

## Deployment durability requirement
`harness.db` and `sessions/` must live on a **persistent volume**, not an ephemeral container filesystem — a redeploy/restart must not wipe durable state. This rules out serverless and points the deploy decision toward a host with attached volumes (e.g. Fly/Render volume).
