# Resume — start here after a context clear

Load this file to restore full project context. It indexes the docs and captures current state.

> **Maintenance protocol:** after every change, update `docs/building.md` (status, checklist, decision log), update `docs/resume.md` if structural, and `git commit` (no co-authors). `.env` is gitignored — never commit secrets.

## What we're building
A **harness** (the framework an AI agent lives inside) for the 24-hour Build Challenge. Demo domain: a **website builder for non-technical small-business owners**. Priorities, in order: **durability, resilience, simplicity.** Stack: **Python + FastAPI in Docker**, OpenRouter free model under a **$1/day** cap.

## Deadlines (from the PRD)
- 1-page Harness Planning Document — **Fri Jun 12, 11:30 PM**.
- Repo URL + deployed URL + `HARNESS.md` + 5-min demo video — **Sat Jun 13, 4:30 PM**.

## Docs index (`docs/`)
- `building.md` — **master checklist + live resume context — load this first after `/clear`.** Per-step status, current state, decision log.
- `v1-spec.md` — source of truth for v1 (data model, 5 checkpoints, 4 alarms, 6 tools, auto post-hook chain, two-worker setup, 429 fallback).
- `http-api.md` — HTTP route reference (path, method, request/response bodies, status codes).
- `test-plan.md` — per-step test gate matrix (one row per build step).
- `mission.md` — challenge brief & requirements (the rubric).
- `harness.md` — design context / spec we follow (**do not edit**).
- `implementation-architecture.md` — full implementation tracking (the deep doc; broader than v1).
- `overview.md` — <500-word high-level; four pillars = **Loop · Tools · Guardrails · Observability**.
- `overview.html` — printable single-page version of the overview.
- `design.md` — Mermaid component diagram + turn-loop flowchart.
- `user-stories.md` — personas + epics.
- `resume.md` — this file.
- `../skills/bootstrap.md` — onboarding skill (captures the Business Brief).
- Reference: `../prd.pdf` (brief), `../harness/*.pdf` (Fired Festival deck).

## Settled architecture (no open decisions)
- **Engine:** durable, resumable, single-process staged-pipeline orchestrator; bounded loop.
- **Storage (two-tier):** core `harness.db` (users, sessions, spend_log) + per-session `sessions/{uuid}.db` (events, checkpoints, material, alarms, run_meta); append-only event log is the source of truth; `parent_session_id` for sub-agents.
- **Worker (decoupled):** swappable behind a data-only `Worker.act(WorkerContext)->WorkerResponse` protocol; stateless; **propose, don't dispose**; config/registry selection; contract tests; `MockWorker` reference.
- **Output contract:** strict JSON envelope `tool_call` / `final` / `escalate`, Pydantic-validated; validate→repair→alarm.
- **Guardrails:** tool allow-list, turn/token/time caps, **$1/day spend ceiling**, sandboxed file/git paths, **human approval every 10 iterations**.
- **Verification:** tiered (deterministic → independent verifier sub-agent → human) + harness self-testing (mock worker, replay/golden runs, fault-injection, property tests).
- **LLM:** OpenRouter, default `nvidia/nemotron-3-ultra-550b-a55b:free`, key in `.env`; configurable.
- **Also:** context compaction; sub-agents (own session); full event + cost logging.
- **Deploy:** FastAPI in Docker, host bind-mount for SQLite data, HTML/JS/CSS landing page = HITL + observability surface.

## v1 production fixes (post-Step 11)
- **`.env` loading:** `load_dotenv()` is called at import time of `harness/api/app.py` and inside `dependencies.build_default_app_context` (defense-in-depth). `OPENROUTER_API_KEY` and the four `MODEL_*` vars are now picked up from `.env` automatically.
- **Polling removed:** `session.html` no longer auto-polls every 2s (which was clobbering form state and `<details>` toggles). Replaced with an explicit "Refresh" link. The `?partial=1` route handler is retained for future smart-partial updates.

## Demo-domain flow (website builder)
onboarding (`bootstrap` skill) → **Business Brief** (defaults baked in; injected into every WorkerContext) → ASCII mockup approval → generate SEO-optimized HTML+CSS+JS + regenerate `sitemap.xml`/`robots.txt`/`llms.txt` each iteration → auto git commit → **final intent audit** vs the brief.
- **Tools:** `ask_user`, `request_approval`, `render_mockup`, `read`/`write`/`list` files, `validate_site`, `regenerate_seo_artifacts`, `git_history`/`git_revert`.
- **Named alarms:** iteration_limit_reached, spend_cap_reached, tool_failed, checkpoint_failed_repeatedly, output_schema_violation, verification_failed, verifier_low_confidence, verifier_disagreement, max_subagent_depth_exceeded, subagent_failed, compaction_stalled, intent_mismatch.

## Next up
See `docs/building.md` for the live status snapshot, current/next step, and 12-step checklist. That file is the source of truth for "where are we" — this section just points you there.
