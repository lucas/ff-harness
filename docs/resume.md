# Resume — start here after a context clear

Load this file FIRST to restore project context, then `docs/building.md` for live build status, then `docs/v1-spec.md` for the source-of-truth spec.

> **Maintenance protocol:** after every change, update `docs/building.md` (status, checklist, decision log), update this file ONLY if structural (new dep, new layer, new pillar), and `git commit` (no co-authors). `.env` is gitignored — never commit secrets.

## What we're building
A **harness** (the framework an AI agent lives inside) for the 24-hour Build Challenge. Demo domain: a **website builder for non-technical small-business owners**. Priorities, in order: **durability, resilience, simplicity.** v1 is functionally complete (Steps 0-11 shipped) with ongoing UX/correctness polish on top.

Stack: **Python 3.14 + FastAPI + raw sqlite3 + Jinja2 + OpenRouter (two models, env-configurable) + Docker**. No async, no ORM, no JS framework. Tests use uv + pytest; mockups use a deterministic Python fallback so the offline suite stays free; live tests are opt-in via `-m live`.

## Where to start after `/clear`
1. **Read these in order**: this file → `docs/building.md` (live status + decision log + 12-step checklist) → `docs/v1-spec.md` (data model, tools, alarms, checkpoints, post-hooks, two-worker setup) → `/Users/elroy/.claude/plans/ignore-any-timelines-they-re-tingly-flame.md` (the canonical plan; ignore the timelines).
2. **Verify state in parallel**: `/Users/elroy/.local/bin/uv run pytest tests/ -m "not live"` and `/Users/elroy/.local/bin/uv run pyright harness/ tests/` and `git log --oneline -10`. All three should be clean / 0 errors / show recent commits.
3. **For new tools**: read the memory at `~/.claude/projects/-Users-elroy-work-claude/memory/tool-call-bubble-rendering.md` — every new worker-callable tool needs a per-tool card renderer in `view_helpers._tool_call_body`; never show JSON to the user.
4. **Cross-check building.md vs git**: ticked boxes (`- [x]`) in building.md should match `git log --oneline | grep "Step "`. If they disagree, trust git and fix the doc.

## Docs index (`docs/`)
- **`building.md`** — live status snapshot + 12-step checklist + decision log. The "where are we" file. Update after every change.
- **`v1-spec.md`** — source of truth for v1: file structure (organized by layer: `models/` / `services/` / `domain/` / `api/` / `templates/`), data model (UUID7 ids, FK semantics, two-tier SQLite), 7 tools, 5 checkpoints, 4 alarms (`iteration_limit_reached`, `spend_cap_reached`, `output_schema_violation`, `tool_failed`), auto post-hook chain (validate → SEO regen → git commit after every successful `write_file`), two-worker stage map, 429 fallback, language-drift retry, `llm_calls` audit table.
- **`http-api.md`** — every route (path, method, request/response shapes, status codes).
- **`test-plan.md`** — per-step test gate matrix.
- **`mission.md`** — the rubric we're scored against (read-only).
- **`harness.md`** — original design context (**do not edit** — constraint doc).
- **`implementation-architecture.md`** — broader-than-v1 design notes; consult per module.
- **`overview.md`** — short high-level (four pillars: Loop / Tools / Guardrails / Observability).
- **`overview.html`** — printable single-page version.
- **`design.md`** — Mermaid component diagrams + turn-loop flowchart.
- **`user-stories.md`** — personas + epics; restaurant (Maria) is the canonical demo path.
- **`resume.md`** — this file.
- **`../skills/bootstrap.md`** — onboarding skill content (verbatim into the chat-worker system prompt).
- Reference (gitignored / not for re-derivation): `../prd.pdf`, `../harness/*.pdf`.

## Settled architecture
- **Engine:** durable, resumable, single-process, sync, HTTP-triggered orchestrator (`harness/services/orchestrator.py::run_until_pause`). All state in SQLite. Crash anywhere → next `/resume` rebuilds from the event log.
- **Storage (two-tier):**
  - Core DB `data/harness.db` — `sessions`, `spend_log` (cross-session $1/day rollup).
  - Per-session DB `data/sessions/{uuid7}.db` — `material`, `checkpoints`, `alarms`, `events`, `llm_calls`. FK-enforced (`PRAGMA foreign_keys = ON`). Every `id` is UUID7 (TEXT, full formatted) so `ORDER BY id` is chronological.
- **Worker (decoupled, swappable):** `Worker` Protocol; `MockWorker` for tests; `LLMWorker(primary, fallback, client, conn, ...)` for prod. **Two stage-mapped instances** wired by the domain bundle:
  - chat worker — `MODEL_CHAT` (default `deepseek/deepseek-v4-flash:free`), fallback `MODEL_CHAT_FALLBACK` (default `deepseek/deepseek-v4-flash`). Used for `bootstrap` / `mockup` stages.
  - code worker — `MODEL_CODE` (default `qwen/qwen3-coder:free`), fallback `MODEL_CODE_FALLBACK` (default empty). Used for the `build` stage AND for `render_mockup`'s themed HTML generation.
- **Output contract:** strict JSON envelope (`tool_call` / `final` / `escalate`), Pydantic-validated. On parse failure → repair retry. On CJK detection → language-drift retry. On exhaustion → `output_schema_violation` alarm + Escalate.
- **429 fallback:** primary 429 → retry once with fallback model → log `is_fallback=1` on the successful row, emit `model_swapped` event.
- **Guardrails:** declared per-session — tool allow-list (7 tools), `data/sites/{uuid}/` sandbox, 10-iter human-approval gate, $1/day spend cap (rolling 24h from `spend_log`), strict envelope validation.
- **Auto post-hooks:** after every successful `write_file` the orchestrator runs validate (html5lib / tinycss2) → regenerate `sitemap.xml`/`robots.txt`/`llms.txt` → `git commit`. NOT worker-callable; harness-enforced.
- **Pillar instances:** Guardrails / Checkpoints / Material / Alarms each implemented in dedicated `services/` modules so the rubric mapping is unambiguous. Worker pillar is exercised in production via the two-worker setup (rubric bonus permanently on).
- **Deploy:** FastAPI in Docker (python:3.14-slim + uv), `./data` bind-mounted. `HARNESS.md` at repo root is the rubric deliverable.

## v1 production fixes (post-Step 11)
Substantial polish on top of the v1 skeleton. Details in `docs/building.md` decision log; one-line summary here:
1. `.env` auto-loaded at app + dependencies-build import time.
2. Polling removed (it was clobbering form state); replaced with explicit Refresh link.
3. Iter/spend cap continuation flow — orchestrator persists a `continuation_approval` pending material so the UI has Approve/Stop buttons to render.
4. `iter_since_approval` resets on ANY `human_resumed` event (not just brief/mockup approvals).
5. Chat-first session UI (chat bubbles projected from events, context-sensitive input area, Details accordion below, alarms + cost at bottom).
6. State-based alarms (`iteration_limit_reached`, `spend_cap_reached`) auto-resolve when their condition no longer holds.
7. `POST /resume` calls `orchestrator.force_continue` to unstick `awaiting_human` sessions — auto-approves continuation_approvals, resets iter counter, flips active.
8. Server-side markdown in agent bubbles via `mistune` (`escape=True` for XSS safety).
9. Input area no longer restates the question/subject/details (chat bubble above is the source of truth).
10. "Other…" button keeps option buttons visible after revealing the textarea.
11. Approval bubbles render as subject-aware semantic cards (no JSON expander).
12. Rewind feature — `POST /sessions/{id}/rewind` truncates everything after a chosen `awaiting_human` event; re-pends material; appends `rewound` event; renders as `.chat-divider` in chat.
13. `render_mockup` themed HTML + sandboxed iframe preview (deterministic Python wireframe by default).
14. `save_business_brief` tool — defense in depth so the brief enters session memory whether the agent uses `request_approval` or `ask_user` for sign-off.
15. Full `llm_calls` audit table — per-session, full request + response payloads, token counts, cost, FK to `worker_input` event; UI shows them in Details accordion with per-row `<details>View</details>` expander.
16. **`render_mockup` LLM-driven via qwen-coder** — code worker generates a real desktop wireframe HTML; deterministic Python renderer is the fallback for tests + LLM failures. Iframe attached BELOW the bubble at 900×640px.
17. Mockup theming bug fixed — auto-seed removed, approved brief persists as a real `business_brief` material.
18. Every tool_call bubble has a semantic card renderer — `save_business_brief` reuses the brief card; `write_file` shows path + bytes + collapsed content; `read_file`/`list_files` are compact cards; generic fallback renders any future tool as a labeled list. **No JSON ever shown to the user.**
19. **Language-drift retry** — CJK ratio check (count ≥ 8 OR ratio ≥ 3%) on every LLM response; rejection retries once with English-strict directive; logged as `language_violation` status in `llm_calls`; terminal failure raises `output_schema_violation` alarm.

## Demo-domain flow (Maria's restaurant — the canonical path)
1. **Bootstrap stage** — agent uses `ask_user` (in batched rounds with options) to collect Business Brief fields. Industry profiles in `skills/bootstrap.md` give defaults the agent uses to pre-fill.
2. **Brief approval** — user approves via `ask_user(options=['Looks good!', …])` OR `request_approval(subject='business_brief', details={...})`. Either way, agent calls `save_business_brief(brief={...})` to persist into session memory. Approval evaluates the `business_brief_confirmed` checkpoint.
3. **Mockup stage** — agent calls `render_mockup(layout_spec=...)` which calls qwen-coder to generate themed HTML matching the brief's palette/name/CTA. Rendered as sandboxed iframe attachment under the agent bubble. `mockup_renders` checkpoint evaluated.
4. **Mockup approval** — `request_approval(subject='mockup')`. Approval evaluates `mockup_approved`.
5. **Build stage** — agent uses `write_file` (driven by qwen-coder) to write HTML/CSS. Each successful write auto-runs validate → SEO regen → git commit; `site_valid` and `seo_artifacts_present` checkpoints evaluated. Loop until `Final` envelope.
6. **Rewind** (any time) — click Rewind on any past `awaiting_human` event in the events table to revert and re-answer.

## Next up
See `docs/building.md` for the live status snapshot and 12-step checklist. That file is the source of truth for "where are we" — this section just points you there. Step 12 (intent-audit checkpoint, alarm severity colors, live model-swap demo) is the only remaining stretch step.
