# Harness v1

## What this is

This repository is a durable, resumable orchestration harness for LLM agents, demonstrated by a website-builder agent that walks a non-technical small-business owner from a vague idea to a published static site. The harness — not the agent — is the artifact under review. Its job is to make constraint-handling invisible to the worker by providing four distinct, declared components: **Guardrails** (allow-lists, sandbox paths, spend caps, and an auto post-hook chain), **Checkpoints** (five deterministic evaluators with explicit pass/fail criteria), **Material handling** (a typed Pydantic envelope plus closed-set material rows persisted to SQLite), and **Alarms** (four named alarm types, each carrying severity, structured context, and a recommended action). The worker boundary is a single `Worker` Protocol; the demo runs **two LLMWorker instances stage-mapped** — a chat model for bootstrap/mockup/approval and a code model for site generation — with a third `MockWorker` used by the test suite, all interchangeable through dependency override.

## The four pillars

| Pillar | Implemented in | Visible as |
|---|---|---|
| Guardrails | `harness/services/guardrails.py`, `harness/services/post_hooks.py`, `harness/services/tools/files.py`, allow-list in `harness/domain/website_builder.py` | Sandbox-escape rejections raised as `tool_failed` alarms; the `post_hook_run` event after every `write_file`; the 10-iteration human-approval gate; the $1/day spend cap |
| Checkpoints | `harness/services/checkpoints.py`, `harness/services/validators.py` | Five named rows in the session detail page (`business_brief_confirmed`, `mockup_renders`, `mockup_approved`, `site_valid`, `seo_artifacts_present`), each with `status` and a `criteria_results` dict |
| Material handling | `harness/models/envelope.py`, `harness/services/store.py`, `harness/services/tools/` | Every tool call, user answer, mockup, and generated site file is persisted as a `material` row with closed-set `direction`+`stage`+`type`; the worker only sees typed `WorkerContext` in and a discriminated `WorkerResponse` out |
| Alarms | `harness/services/alarms.py`, `harness/models/enums.py` (closed `AlarmType` set) | Alarm rows with `{type, severity, context, recommended_action, stage}`; raised by guardrails, the tool dispatcher, checkpoint failures, and the LLM client's rate-limit handler |

Each pillar is a separate module that imports nothing from the worker layer; the worker only imports the envelope types. That separation is what lets the same orchestrator drive a `MockWorker` in tests and two `LLMWorker` instances in production with zero changes.

## Module map

Files are grouped by layer. Upper layers may import from lower; the reverse is forbidden. Tests mirror this structure under `tests/`.

### Layer 1 — Data (`harness/models/`)

| File | Purpose |
|---|---|
| `ids.py` | `new_id()` — UUID7 (time-ordered) ids stringified, used for every primary key |
| `enums.py` | Closed sets: `EventType`, `MaterialType`, `AlarmType`, `CheckpointName`, `Stage`, `Severity`, `Direction`, `SessionStatus` |
| `envelope.py` | Pydantic models: `Message`, `WorkerContext`, discriminated `ToolCall` / `Final` / `Escalate` |
| `ddl.py` | `CREATE TABLE` strings (FK-safe order) and the `PRAGMA foreign_keys = ON` setup |

### Layer 2 — Service (`harness/services/`)

| File | Purpose |
|---|---|
| `store.py` | All SQL; opens the two SQLite DBs (core + per-session); pure persistence |
| `worker.py` | `Worker` Protocol + `MockWorker` (scripted-response stub used by tests) |
| `guardrails.py` | Pure functions: `is_tool_allowed`, `is_path_safe`, `turn_cap_exceeded`, `spend_cap_exceeded` |
| `alarms.py` | `raise_alarm()` — persists the row and appends an `alarm_raised` event |
| `validators.py` | `validate_html` (html5lib), `validate_css` (tinycss2), `validate_sitemap_xml` (stdlib) |
| `post_hooks.py` | The validate → SEO regen → git commit chain that runs after every `write_file` |
| `checkpoints.py` | Registry + the five deterministic evaluator functions |
| `orchestrator.py` | `run_until_pause(session_id, config)` — the loop and state machine; no SQL, no HTTP, no LLM calls |
| `llm.py` | `OpenRouterClient` — HTTP, spend logging, `RateLimited` exception on HTTP 429 |
| `llm_worker.py` | `LLMWorker(primary, fallback, ...)` — implements `Worker`, auto-swaps to `fallback` on 429 |
| `tools/__init__.py` | `ToolContext`, `ToolResult`, `dispatch(name, args, ctx)` |
| `tools/user.py` | `ask_user`, `request_approval` — HITL escalation tools that write a pending material |
| `tools/files.py` | `read_file`, `write_file`, `list_files` — all sandboxed under `data/sites/{session_id}/` |
| `tools/mockup.py` | `render_mockup` — deterministic ASCII layout renderer |

### Layer 3 — Domain + API (`harness/domain/`, `harness/api/`)

| File | Purpose |
|---|---|
| `domain/website_builder.py` | System prompt, tool allow-list, checkpoint set, stage→worker map, the Maria's restaurant seed brief, `make_orchestrator_config()` factory |
| `api/app.py` | FastAPI app — five JSON routes + HTML routes that render the templates |
| `api/dependencies.py` | `AppContext` + `get_app_context` Depends; the single seam tests use to inject a `MockWorker` factory |

### Layer 4 — Frontend (`harness/templates/`)

| File | Purpose |
|---|---|
| `_base.html` | Page chrome + 2-second polling JS |
| `index.html` | Session list + "New session" form |
| `session.html` | Event timeline, checkpoints, alarms, spend summary, generated-site iframe |
| `_session_main.html` | Polled partial that swaps the main panel without a full page reload |
| `awaiting.html` | Form rendered when a `pending_question` material is open |

## Worker boundary + swappability

The `Worker` Protocol in `harness/services/worker.py` is a single method: `act(ctx: WorkerContext) -> WorkerResponse`. The orchestrator never knows which model — or even which class — is on the other side. Tests pass a `MockWorker` constructed with a list of pre-baked envelopes; production uses `LLMWorker` over OpenRouter via `harness/services/llm.py`.

In production the demo runs **two `LLMWorker` instances, stage-mapped** in `harness/domain/website_builder.py::make_worker_for_stage`:

- **Chat worker** — default model `deepseek/deepseek-v4-flash:free`, fallback `deepseek/deepseek-v4-flash`. Used for the `bootstrap` and `mockup` stages (brief gathering, layout design, approval requests).
- **Code worker** — default model `qwen/qwen3-coder:free`, fallback configurable. Used for the `build` stage (`write_file` of HTML and CSS).

On HTTP 429 the LLM client raises `RateLimited(model)`; `LLMWorker.act()` catches it, retries once against `fallback`, logs the second call to `spend_log` with `is_fallback=1`, and appends a `model_swapped` event so the swap is visible in the UI. If the fallback is also rate-limited or unset, a `tool_failed` alarm is raised and the session pauses. Swapping the entire worker layer to a hypothetical Anthropic or local model would mean writing one new class implementing `Worker` and adjusting the factory in `website_builder.py` — the orchestrator, guardrails, checkpoints, and alarms see no change.

## Demo flow (Maria's restaurant)

The canonical demo is a six-stage flow against the restaurant seed brief in `RESTAURANT_SEED_BRIEF`:

1. **Create session** — `POST /sessions` writes a row in the core DB and seeds the brief as a `business_brief` material so the worker has context from turn 1.
2. **Brief approval** — chat worker reviews the seeded brief and calls `request_approval(subject='business_brief')`. The session moves to `awaiting_human`; the operator approves via `awaiting.html`; the `business_brief_confirmed` checkpoint passes; stage advances to `mockup`.
3. **Mockup render** — chat worker emits a `layout_spec` material and calls `render_mockup`, producing an ASCII mockup with named regions. The `mockup_renders` checkpoint passes.
4. **Mockup approval** — worker calls `request_approval(subject='mockup')`; operator approves; `mockup_approved` passes; stage advances to `build`.
5. **Site write — failure → fix loop.** Code worker calls `write_file('index.html', ...)`. The post-hook chain runs automatically: validate → SEO regen → git commit. On a first iteration with a minimal skeleton, the `site_valid` checkpoint typically fails on one or more of `has_title` / `has_meta_viewport` / `has_h1`; a `tool_failed` alarm is raised; the worker sees the failure in `state.last_alarm` and `state.last_checkpoint` on its next turn and re-writes the file with the missing element. The second pass clears `site_valid` and `seo_artifacts_present`. **This is the rubric's "behavior changes meaningfully from feedback" Must — the worker reads structured alarm context written by the harness and adapts.**
6. **Final** — worker emits a `final` envelope; session status flips to `completed`; the UI shows the iframe of the generated site, both checkpoint rows (fail then pass), the alarm, the total spend (well under $1), and a `model_swapped` badge if a 429 occurred during the run.

## How to run

### Local (recommended for dev)

Prerequisites: `uv` (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`) and Python 3.14 (managed by uv).

```bash
cp .env.example .env                            # then fill in OPENROUTER_API_KEY
uv sync                                          # installs all deps including dev
uv run pytest -m "not live"                      # 215 tests, fully offline
uv run uvicorn harness.api.app:app --reload      # serves on :8000
```

Visit `http://localhost:8000/` and click "New session" to start the Maria's restaurant flow.

### Docker

Prerequisites: Docker Desktop (or any Docker engine) and a populated `.env` on the host. The image is the Python 3.14 slim base with uv installing only the production dependency set.

```bash
cp .env.example .env                            # then fill in OPENROUTER_API_KEY
docker compose up --build
```

Visit `http://localhost:8000/`. Session DBs and generated sites are persisted to `./data/` on the host via a bind mount — `docker compose down` is non-destructive; the next `docker compose up` rehydrates the full session list.

## Configuration

Every env var consumed by the harness. Defaults below match `harness/domain/website_builder.py`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | OpenRouter auth for both chat and code workers |
| `MODEL_CHAT` | no | `deepseek/deepseek-v4-flash:free` | Primary chat model (bootstrap, mockup, approval) |
| `MODEL_CHAT_FALLBACK` | no | `deepseek/deepseek-v4-flash` | Paid chat fallback after a 429 on `MODEL_CHAT` |
| `MODEL_CODE` | no | `qwen/qwen3-coder:free` | Primary code model (`build` stage `write_file` calls) |
| `MODEL_CODE_FALLBACK` | no | empty | Paid code fallback; if unset, a 429 on the primary raises `tool_failed` |
| `SPEND_CAP_USD` | no | `1.0` | Daily spend cap (USD) before `spend_cap_reached` alarm halts the loop |
| `TURN_CAP` | no | `10` | Iterations without human approval before `iteration_limit_reached` alarm |

## Rubric mapping

Each row of the mission rubric (from `docs/mission.md`) traced to where it is satisfied in the code and visible in the demo.

| Rubric item | Tier | Satisfied in |
|---|---|---|
| All four pillars implemented and demonstrably separate from the worker | Must | `harness/services/guardrails.py`, `checkpoints.py`, `alarms.py`, `tools/`, plus `models/envelope.py` for material; worker layer is `services/worker.py` + `services/llm_worker.py` and imports none of them |
| Agent's behavior changes meaningfully based on guardrail or checkpoint feedback | Must | The `site_valid` failure on iteration 1 of the build stage feeds back via `WorkerContext.state.last_alarm` and `state.last_checkpoint`; the worker reads the missing-tag criterion and re-writes the file (Demo flow §5) |
| Guardrails declared, not implicit | Must | `ALLOW_LIST` in `domain/website_builder.py`; pure functions in `services/guardrails.py`; sandbox enforcement in `services/tools/files.py`; the auto post-hook chain in `services/post_hooks.py` |
| Checkpoints have explicit pass/fail criteria | Must | Each of the five evaluators in `services/checkpoints.py` returns a `criteria_results` dict with named booleans; `status='pass'` iff all true |
| Alarms produce structured output (named types + context + severity + recommended action) | Must | Closed `AlarmType` set in `models/enums.py`; `alarms.raise_alarm` persists `{type, severity, context, recommended_action}`; all four types defined in the spec |
| Runs on a real input from the engineer's own work at demo time | Must | The demo seeds `RESTAURANT_SEED_BRIEF` for a hypothetical small-business owner; the live demo additionally allows the operator to override fields via the bootstrap `ask_user` flow |
| `HARNESS.md` covering architecture and design | Must | This file |
| Swappable agent interface — drop-in agent needs no harness changes | Should | The `Worker` Protocol in `services/worker.py`; `MockWorker` (tests) and two `LLMWorker` instances (prod) coexist; the API's `AppContext` exposes `worker_for_stage_factory` as the single injection seam |
| Checkpoint results persisted — replay from any checkpoint forward | Should | The `checkpoints` table in the per-session DB stores every evaluation with `criteria_results` and `material_id`; the event log lets `run_until_pause` resume from the last completed turn after a crash |
| Human-in-the-loop escalation | Should | `ask_user` / `request_approval` tools write a `pending_question` material with `pending=1` and flip the session to `awaiting_human`; `POST /sessions/{id}/answer` resumes the loop |
| A second worker swapped in during the demo to prove portability | Bonus | Two stage-mapped `LLMWorker` instances always — chat for `bootstrap`/`mockup`, code for `build`. The `model_swapped` event row makes the swap visible whenever a 429 triggers the fallback |

## Tests

| Suite | Command | Count |
|---|---|---|
| Offline (full suite) | `uv run pytest -m "not live"` | 215 passed |
| Live (opt-in) | `uv run pytest -m live` | 1 live test, requires `OPENROUTER_API_KEY`, kept under $0.05 per run |
| Static analysis | `uv run pyright harness/ tests/` | 0 errors |

The offline suite covers every layer: UUID7 ordering and DDL round-trips, envelope discriminated-union parsing, all four guardrail predicates, every tool happy + failure path, the post-hook chain ordering and partial-failure semantics, all five checkpoint evaluators on good and bad fixtures, the full mock-driven orchestrator session (every event type, every checkpoint row, all four alarms raisable, crash-resume identical terminal state), 429→fallback and envelope-repair paths in `LLMWorker`, every JSON route, every template render.

## Where the docs live

| File | Purpose |
|---|---|
| `docs/v1-spec.md` | The source-of-truth v1 spec — file structure, data model, envelope, event/material/checkpoint/alarm sets, tools, post-hooks, loop semantics, integration points, demo flow |
| `docs/http-api.md` | Per-route reference for the five JSON routes |
| `docs/test-plan.md` | Per-step test gates and acceptance criteria |
| `docs/building.md` | Live build state — the 12-step checklist, current step, decision log |
| `docs/resume.md` | Post-`/clear` entry point for resuming work without losing context |
| `docs/mission.md` | The rubric this `HARNESS.md` is graded against |
| `docs/harness.md` | The original Build Challenge brief and design state |
| `docs/implementation-architecture.md` | Architecture-level decisions that pre-date the v1 spec |
| `docs/design.md`, `docs/overview.md`, `docs/user-stories.md` | Background context on personas and product framing |
