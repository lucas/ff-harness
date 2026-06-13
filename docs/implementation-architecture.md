# Implementation Architecture

How we will build the harness. A 500-word high-level summary lives in `overview.md`; a component flowchart lives in `design.md`; product user stories live in `user-stories.md`; the spec/design context lives in `harness.md` (do not edit — it's the document we follow); the challenge brief and requirements live in `mission.md`. This file tracks implementation decisions only.

**Design priorities (in order):** durability (state survives crashes), resilience (keeps working despite failures), simplicity. Language: **Python**. The harness is the focus; the agent/worker is a thin pluggable thing at the edge.

## Decision status
- **Settled:** Python; durability/resilience-first; simple/single-process; staged-pipeline execution; two-tier SQLite storage; swappable worker behind a minimal protocol; declared per-domain bundle (guardrails + tools + skills + checkpoints); loop-approval gate at 10 iterations; tool registry + dispatch choke point; skill registry; OpenRouter LLM client (configurable model, default free Nemotron, $1/day spend cap); context compaction; sub-agents with isolated sessions; full event + cost logging; tiered verification (deterministic → independent verifier sub-agent → human) + harness self-testing; strict JSON worker-output envelope validated with Pydantic; FastAPI-in-Docker deploy with bind-mounted SQLite volume and HTML/JS/CSS landing page.
- **Settled (demo domain):** a **website builder for non-technical users** — clarify intent → confirm an ASCII mockup → generate SEO-optimized HTML+CSS+JS (+ `sitemap.xml` each iteration), auto-committed to a local git repo. See *Demo domain* below.
- **Open:** none — architecture complete; next deliverable is the 1-page planning document (due Friday June 12, 11:30 PM).

## Core stance
The harness is a **durable, resumable run orchestrator**. It drives a worker through a sequence of stages, wraps every step in the four pillars, and persists enough state that any run can be **resumed after a crash** and **replayed from any checkpoint**. The worker is untrusted and interchangeable.

> The run is a durable state machine; the worker is a function the state machine calls.

The orchestrator, store, and four pillars are **domain-agnostic core**. What makes a given agent is a **declared per-domain bundle** — its guardrails, tools, skills, and checkpoints. Swapping that bundle (and the worker) yields a new agent with no changes to the core. The three configurable layers are distinct by intent: **guardrails constrain** (enforced), **tools empower** (callable), **skills guide** (advisory context).

## Demo domain — website builder (non-technical users)
The real input the harness runs on at demo time, and a concrete instance of the per-domain bundle. A user submits a basic idea or change; the harness runs onboarding (the `bootstrap` skill) to capture intent as a **Business Brief**, confirms layout via an ASCII mockup, then generates a simple, **SEO-optimized** HTML+CSS+JS page and regenerates SEO artifacts (`sitemap.xml`, `robots.txt`, `llms.txt`) after each iteration — every change auto-committed to a local git repo for versioning. A single structured `layout_spec` drives both the ASCII mockup and the built HTML, so what the user approves is what ships. A final **intent audit** verifies the finished site against the Business Brief before it's considered done.
- **Tools (agent-callable):** `ask_user` (clarifying questions → HITL), `request_approval` (present ASCII mockup, await approve/revise → HITL), `render_mockup(layout_spec)` (deterministic ASCII wireframe), `read_file` / `write_file` / `list_files` (sandboxed to `data/sites/{session}/`). `ask_user`/`request_approval` are the envelope's `escalate` action surfaced as tools.
- **Harness-automatic each iteration (NOT worker tools):** `validate_site` → `regenerate_seo_artifacts` → `git_commit`. The harness enforces this chain after every successful `write_file`; the worker cannot call them directly. See `docs/v1-spec.md` § Auto post-hook chain.
- **Harness-automatic each iteration:** `regenerate_seo_artifacts`, `validate_site`, then `git commit` — guaranteed, not left to the agent.
- **Optional/advanced:** `lighthouse_audit` (SEO score via headless Chrome), `screenshot` (preview for the human UI; the free model isn't vision-capable), `delete_file`.
- **Validators:** BeautifulSoup + lxml/html5lib (HTML & sitemap), tinycss2 (CSS), a JS parser or `node --check` (JS).
- **Skills:** `bootstrap` (onboarding → Business Brief), clarifying-question technique, simple web-design & SEO guidance.
- **Business Brief (in context):** onboarding captures a structured brief (name, industry, contact, locations, hours, aesthetic, colors, audience, pages, CTA, socials) with industry-aware defaults; confirmed by the user, persisted (`run_meta`/material), and injected into every `WorkerContext` so the build stays accurate.
- **Checkpoints:** Business Brief confirmed; ASCII mockup confirmed by the user; generated HTML/CSS/JS valid; **SEO checks pass** (title, meta description, single H1 + heading hierarchy, semantic tags, `lang`, viewport, self-referencing canonical, Open Graph/Twitter, JSON-LD matching visible text, image `alt`); `sitemap.xml`/`robots.txt`/`llms.txt` regenerated & valid; no broken internal links (deterministic Tier 1); **final intent audit** — finished site verified against the Business Brief (name/contact/locations/services present & correct = deterministic; aesthetic/tone matches intent = verifier sub-agent), mismatches raise `intent_mismatch`.
- **HITL:** the clarifying questions and mockup confirmation are the natural `awaiting_human` escalation points — the harness stops and asks rather than guessing (the PRD's stop-and-ask Should).
- **Versioning:** automatic local git commits give a durable, revertable history of generated artifacts, complementing the event log.

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
| Checkpoints | `checkpoints.py` — named gates with explicit pass/fail criteria, run as a tiered verification ladder → persisted result | Durability + resilience |
| Material handling | `material.py` — typed input/output ports (incl. the JSON output envelope); validate on ingress/egress; serialize to the session store | Durability + resilience |
| Alarms | `alarms.py` — named types, severity, context, recommended action; routed to handlers (log / escalate / halt) | Resilience |

## Swappable worker boundary (decoupling)
The harness depends on an **abstraction**, never a concrete agent (dependency inversion). The entire surface is one method — `Worker.act(context: WorkerContext) -> WorkerResponse` — across a **data-only, serializable contract**. Dropping in a different agent is a config change, not a harness change (the swappable-agent Should + portability Bonus).

- **`WorkerContext` in / `WorkerResponse` out** are plain Pydantic models — context (system prompt, messages, available tool schemas, relevant skills, state summary) in; the `tool_call`/`final`/`escalate` envelope out. No harness objects, DB handles, or provider types cross the line. Being serializable, a worker can even run out-of-process or in another language (optional; in-process is the simple default).
- **Stateless worker.** The harness assembles the full context each turn (material handling + compaction) and owns all state — the loop, iteration/budget counters, persistence, guardrails, checkpoints, alarms. The worker is a pure function: context → next action.
- **Propose, don't dispose.** The worker only *requests* a tool call; the harness validates (allow-list + arg schema), executes, and feeds the result back. The worker never touches the filesystem, git, network, or store.
- **Capabilities passed in, not assumed.** Tools and skills arrive in the context (the per-domain bundle), so the worker hardcodes no tool names and the same worker works across domains.
- **Provider adapters.** Concrete workers (`OpenRouterWorker`, `MockWorker`, a future `AnthropicWorker`) implement the protocol; provider specifics live in `llm.py`/the adapter, not the harness.
- **Selection via config + registry.** `worker_id` in config → a factory builds the worker; swapping is a config change, and a second worker can be dropped in live.
- **Locked by contract tests.** A shared suite every worker must pass; `MockWorker` is the reference implementation and also powers the Track-B harness tests.

Tight-coupling smells to avoid: the worker importing `store`/`guardrails`/`checkpoints`; the worker running the loop or counting iterations; the worker executing tools itself; the harness branching on worker type; provider-specific fields leaking into the contract.

## Worker output contract (strict JSON envelope)
Every worker turn must return one JSON object, discriminated on `type` — the entire contract, kept deliberately tiny:
- `{ "type": "tool_call", "tool": <name>, "args": { ... } }`
- `{ "type": "final", "result": <string or object> }`
- `{ "type": "escalate", "reason": <why a human is needed> }`

Validated with **Pydantic** (already in the FastAPI stack — no new dependency). Per turn: parse JSON → validate the envelope → if `tool_call`, validate `args` against that tool's registered parameter schema (reuses the tool registry) → dispatch on `type`. Validation failure is **fail-as-data**: a bounded "repair" retry feeds the error back to the model; if it still fails, raise `output_schema_violation` and escalate/halt.

Enforcement prefers OpenRouter's structured output (`response_format` json_schema) when the model supports it, else system-prompt-instructed JSON + validate + repair (the free Nemotron will likely need the fallback). This keeps the loop *call → validate → dispatch* and yields the cheapest deterministic verification gate for free.

## Tools (capabilities the agent can call)
A declared **tool registry**. Each tool: `name`, `description` (what the model reads to decide), typed `parameter schema`, `executor` (the real function), and a `result contract` (parseable output; errors returned as structured data, never crashes). Per-tool metadata: allow-list membership, `requires_approval`, timeout, max_retries, idempotent.

All calls pass through one **dispatch choke point** that: rejects non-allow-listed tools (guardrail), validates args (material handling), enforces timeout + retries/backoff (resilience), returns results/errors as data, traces the call, and raises alarms on failure. `tools.py` holds the registry + dispatch; individual executors can grow into a `tools/` package.

## Skills (guidance that shapes the agent)
A declared **skill registry** of advisory guidance — *not* enforced like guardrails. Each skill: `name`, `description` (one-line, used to select relevance), and a `body` (the procedure/guidance). Skills are stored as markdown with frontmatter and loaded by `skills.py`; the material-handling layer injects relevant skills into the worker's context when assembling a stage.

The contrast that keeps the configurable layers clean: **guardrails constrain** (enforced), **tools empower** (callable), **skills guide** (advisory context).

## LLM provider & cost control
LLM calls go through OpenRouter (OpenAI-compatible API, base `https://openrouter.ai/api/v1`). The API key lives in `.env` (`OPENROUTER_API_KEY`), loaded from the environment — never logged, never committed.

- **Two-worker setup (v1 default).** Stage-mapped via `domain.worker_for_stage(stage)`:
  - **`chat` worker** — `MODEL_CHAT` primary (default `deepseek/deepseek-v4-flash:free`), `MODEL_CHAT_FALLBACK` (default `deepseek/deepseek-v4-flash` — the paid variant). Used for bootstrap, brief confirmation, mockup design, escalation/clarification.
  - **`code` worker** — `MODEL_CODE` primary (default `qwen/qwen3-coder:free`), `MODEL_CODE_FALLBACK` (default empty — set to a paid coder when one is configured). Used for site-generation turns.
  - Env vars: `MODEL_CHAT`, `MODEL_CHAT_FALLBACK`, `MODEL_CODE`, `MODEL_CODE_FALLBACK`, `OPENROUTER_API_KEY`. Model ids are env-configurable; no defaults reference Nemotron in v1.
- **429 → fallback auto-swap.** When the primary returns HTTP 429, `llm.py` raises `RateLimited`; `LLMWorker` retries once with the configured fallback (if any), logs the call to `spend_log` with `is_fallback=1`, and the orchestrator appends a `model_swapped` event (`{from, to, reason: 'rate_limited'}`) so the swap is visible in the UI. If the fallback also 429s or no fallback is set, a `tool_failed` alarm is raised.
- **Spend ceiling is a declared guardrail.** Default cap **$1/day**, configurable. Before each call the guardrail sums today's `spend_log` (core DB) and blocks if the cap would be exceeded → `spend_cap_reached` alarm → escalate. Free models report $0 but the path is always enforced.
- `llm.py` is provider-agnostic; OpenRouter is one implementation. Each response yields tokens in/out, cost, latency, and the exact model id used — captured per call to `spend_log` (with `is_fallback`) and the event log.

## Context management & compaction
> **Out of scope for v1 — deferred to Tier 2; see `docs/v1-spec.md`.**

To continue past a model's hard context limit, `context.py` manages the worker's working context and **compacts** when it nears a configurable threshold (fraction of the model's window).

- On breach: summarize older turns/tool-results into a compact summary that preserves the task, decisions, current state, open items, and last checkpoint; replace the verbose history with it. The summarization is itself an LLM call (cheapest configured model).
- **Compaction never loses data.** The full record stays in the durable `events` log (source of truth); only the *working context* shrinks. The compaction summary is persisted as an event, so replay stays deterministic.
- A `context_compacted` event logs before/after token counts; repeated compaction with no progress raises an alarm.

## Sub-agents (nested sessions)
> **Out of scope for v1 — deferred to Tier 2; see `docs/v1-spec.md`.**

A sub-agent is the **same harness loop running in its own session** (`sessions/{uuid}.db`) with fresh context, spawned by a parent worker via a `spawn_subagent` tool. Only the sub-agent's final result re-enters the parent context — its internal turns stay in its own session, so the parent's context is preserved (complements compaction).

- Lineage: the child `sessions` row sets `parent_session_id`; the parent logs `subagent_spawned` (child id) and `subagent_result` events. The session tree is fully durable and replayable.
- Resilience: a sub-agent failure returns to the parent as structured data (fail-as-data); the parent decides. Recursion depth is bounded by a declared `max_subagent_depth` guardrail; the daily spend cap is global across the whole session tree.
- `subagent.py` owns spawn/await; each sub-agent can carry its own skills/tools/worker.

## Verification strategy
> **v1 ships Tier 1 (deterministic) checkpoints only; verifier sub-agent (Tier 2) deferred. See `docs/v1-spec.md` § The 5 checkpoints.**

Two tracks: confirming the worker's output (A) and confirming the harness itself (B).

**Track A — output verification (tiered, confidence-gated).** A checkpoint runs tiers in order, stopping as soon as one can decide; cost rises with tier.
1. **Deterministic oracles (free, first):** JSON-envelope validity, tests, type-check/LSP, lint, schema/format, declared invariants. Most gates resolve here at $0.
2. **Independent verifier sub-agent (semantic gates only):** spawned via `subagent.py` in its own session, ideally a *different model* than the worker (external verification beats self-correction). Bias mitigations: explicit pass/fail rubric, required cited evidence, low temperature, randomized/masked order, verbosity penalty. Gated by the spend ceiling — skipped + alarmed if the cap would be exceeded.
3. **Human escalation:** on low verifier confidence or high-stakes gates, via the existing `awaiting_human` path.

Every tier's verdict + evidence persists as a `CheckpointResult`; failures raise alarms.

**Track B — harness self-testing.** Leverages the append-only event log: a **mock deterministic worker** (no LLM/cost), **replay/golden-run tests** (re-run a recorded session, assert identical state), **fault-injection** (kill mid-run, resume, assert consistency + no double-applied effects), and **property/idempotency tests** on the state machine. Optional statistical-significance gating for agent-quality deltas (don't ship on noise).

`verification.py` holds the deterministic verifiers + the verifier-sub-agent driver; `checkpoints.py` orchestrates the tier ladder; a `verification` skill guides the verifier sub-agent; `tests/` holds the Track B suite.

## Logging & observability
The `events` log is also the audit trail — we log thoroughly, structured, per session:

- Each worker turn: assembled input context and raw output.
- Each LLM call: model, tokens in/out, **cost**, latency.
- Each tool call: name, args, result or error, duration.
- Each user/agent input, guardrail decision, checkpoint result, alarm, approval, compaction, and sub-agent spawn/result.

Cost is recorded per call in `events` (full detail) and mirrored to the core `spend_log` (lightweight, cross-session) so the daily spend guardrail is a single query. Secrets are redacted; logging captures full payloads (durability) even though the live context is compacted.

**Named alarm types so far:** `iteration_limit_reached`, `spend_cap_reached`, `tool_failed`, `checkpoint_failed_repeatedly`, `output_schema_violation`, `verification_failed`, `verifier_low_confidence`, `verifier_disagreement`, `max_subagent_depth_exceeded`, `subagent_failed`, `compaction_stalled`, `intent_mismatch` — each with severity, context, and recommended action.

> **v1 ships only 4 of these alarms:** `iteration_limit_reached`, `spend_cap_reached`, `output_schema_violation`, `tool_failed`. The rest are deferred (they depend on the verifier sub-agent, sub-agents, context compaction, or the intent-audit checkpoint — all Tier 2). See `docs/v1-spec.md` § The 4 alarms.

## Module layout
> **v1 file structure is organized by layer (`models/`, `services/`, `domain/`, `api/`, `templates/`) — see `docs/v1-spec.md` § File structure (by layer) for the shipped layout.** The flat layout below is the original architectural sketch and is retained here for historical context.

```
app.py              # FastAPI: harness HTTP API + static serving
static/             # landing page (HTML/JS/CSS, no build) — HITL + observability surface
Dockerfile
docker-compose.yml  # bind-mount host folder → SQLite data
data/               # host bind-mount target: harness.db + sessions/
skills/             # skill markdown (e.g. bootstrap, verification)
tests/              # mock worker, replay/golden runs, fault-injection, property tests
harness/
  orchestrator.py   # the engine: resume run, drive stages, bounded loop
  store/
    core.py         # users + sessions index (control plane)
    session.py      # per-session DB: events, checkpoints, material, alarms (data plane)
  guardrails.py     # declared policies + interception points
  checkpoints.py    # gate definitions, tier orchestration, results
  material.py       # typed I/O ports, validation, JSON output envelope
  alarms.py         # alarm types, severity, recommended actions, routing
  tools.py          # tool registry + dispatch choke point (allow-listed, validated)
  skills.py         # skill registry + relevance loading (advisory guidance)
  verification.py   # deterministic verifiers + verifier-sub-agent driver
  llm.py            # provider-agnostic LLM client; OpenRouter impl; returns usage + cost
  context.py        # context-window manager + compaction (continue past the hard limit)
  subagent.py       # spawn/await nested sessions (own DB, fresh context)
  worker.py         # the Worker protocol + default LLM worker (uses llm.py)
  config.py         # declarative limits, model/provider, caps, tool/skill declarations
```

**Engine in one breath:** load-or-resume run → for each stage from the resume point → guardrails pre-check → run stage with retries/timeout/fail-as-data (compact context near the limit; pause for human approval every 10 iterations) → guardrails post-check → checkpoint evaluate → persist result → on fail: alarm + (retry | escalate | halt); on pass: advance.

## Deployment
A single **FastAPI app in a Docker container** serves both the harness HTTP API and a static landing page. A **host folder is bind-mounted** into the container; `harness.db` and `sessions/` point at the mounted path, so rebuilds/redeploys never wipe durable state and the container stays disposable.
- SQLite-on-volume: keep **WAL** on (WAL/SHM sidecars sit beside each `.db` on the same volume); the single-process orchestrator + one writer per session DB avoid contention; file locking is solid on a Linux host volume but can be flaky on macOS Docker Desktop bind mounts (local-dev caveat).
- The **landing page** (basic HTML/JS/CSS, no build step) doubles as the **HITL + observability surface**: session list/status, `awaiting_human` escalations with alarm context, approve/deny actions that drive resume, and a run/cost view from `events` + `spend_log`.
