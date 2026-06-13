# Test Plan — Harness v1

This is the per-step test-gate matrix. Each row mirrors a step in the Build Order in `/Users/elroy/.claude/plans/ignore-any-timelines-they-re-tingly-flame.md`. A step is "done" only when its gate is green and the "Cannot proceed unless" sentence is true. Tests live under `tests/`, mirroring the `harness/` layer layout.

## Test conventions

- **Runner:** `pytest`, configured in `pyproject.toml`.
- **Markers:**
  - `live` — opt-in tests that make real OpenRouter HTTP calls. Skipped by default; run with `-m live` (requires `OPENROUTER_API_KEY`). Counts against the per-model daily quota and the $1/day spend cap.
  - `slow` — the full demo flow in Step 11; deselected by default.
- **Fixtures (`tests/conftest.py`):** `tmp_core_db` (fresh `harness.db` under `tmp_path`), `tmp_session_db_factory` (mints a session + per-session DB path), `tmp_sandbox` (empty `tmp_path` with `git init`), `mock_worker_factory` (builds `MockWorker` from scripted `WorkerResponse`s).
- **HTTP testing.** `fastapi.testclient.TestClient`. Workers are swapped via FastAPI dependency overrides — no LLM calls unless `-m live`.
- **No real LLM calls before Step 8.** Steps 0–7 are deterministic; Step 8 non-`live` tests stub `llm.chat()`.
- **Isolation.** Every test gets its own `tmp_path` DBs.
- **Parameterized SQL only.** Store tests include at least one injection-shaped input assertion.

## How to run all gates locally

```bash
uv run pytest                          # offline only (default)
uv run pytest -m "live or not live"    # + live LLM calls (requires OPENROUTER_API_KEY)
uv run pytest -m live                  # only live tests
uv run pytest -m "slow or not slow"    # + Step 11 demo flow
```

`uv sync` resolves dependencies; `pytest --collect-only` is the Step 0 gate.

## Per-step gate matrix

| Step | Name | Test files | Smoke command | Proving assertion | Manual verification | Cannot proceed unless |
|---|---|---|---|---|---|---|
| 0 | Docs first | (none — `tests/` is empty scaffolding) | `uv run pytest --collect-only && uv sync` | `pytest` finds 0 tests without error; `uv sync` resolves the full dep set in `pyproject.toml`. | Peer can read `docs/building.md` + `docs/v1-spec.md` and predict Step 1's output. | All four upfront docs and all scoped doc edits are written and reviewed. |
| 1 | UUID7 helper + DDL + Store | `tests/models/test_ids.py`, `tests/services/test_store.py` | `uv run pytest tests/models/test_ids.py tests/services/test_store.py` | `new_id()` returns valid UUID7s; 100 sequential ids are strictly chronologically ordered; full CRUD roundtrip on all 6 tables; inserting an event with a non-existent `material_id` raises `IntegrityError` (FKs enforced); `recent_spend_today_usd` sums correctly across sessions including `is_fallback=1` rows. | Open a tmp DB in `sqlite3` shell, `PRAGMA foreign_keys;` returns 1. | Every store function the upper layers need is implemented and round-trip tested. |
| 2 | Envelope + Worker protocol + MockWorker | `tests/models/test_envelope.py`, `tests/services/test_mock_worker.py` | `uv run pytest tests/models/test_envelope.py tests/services/test_mock_worker.py` | Each of `ToolCall`/`Final`/`Escalate` parses; the discriminated union rejects bad `type` values and bad shapes with a Pydantic `ValidationError`; `MockWorker` pops responses in scripted order and raises a clear error when exhausted. | none | Envelope is the only shape the orchestrator will accept from a worker. |
| 3 | Guardrails + Alarms | `tests/services/test_guardrails.py`, `tests/services/test_alarms.py` | `uv run pytest tests/services/test_guardrails.py tests/services/test_alarms.py` | `is_tool_allowed`, `is_path_safe`, `check_turn_cap`, `check_spend_cap_today` return the expected booleans across a fixture matrix (including symlink/`..` path-escape attempts); `raise_alarm` for each of the 4 alarm types writes the row with the right severity, populates `context` per the spec, AND appends a paired `alarm_raised` event. | none | The pillar that enforces the spend cap and sandbox is provably correct in isolation. |
| 4 | Tools + dispatcher | `tests/services/test_tools.py` | `uv run pytest tests/services/test_tools.py` | Every tool tested on happy + one failure path; `dispatch` denies a non-allow-list tool and raises a `tool_failed` alarm; `write_file` refuses paths outside `tmp_sandbox`; `ask_user`/`request_approval` persist a `pending_question` material and return the `paused` sentinel; `render_mockup` output is deterministic for fixed input. | none | Every worker-callable surface is provably guardrailed. |
| 5 | Validators + post-hooks | `tests/services/test_validators.py`, `tests/services/test_post_hooks.py` | `uv run pytest tests/services/test_validators.py tests/services/test_post_hooks.py` | Known-bad HTML/CSS fixtures fail with the specific error keys named in the spec; known-good fixtures pass; `post_hooks.run` on a fresh `git init`ed sandbox writes `sitemap.xml`/`robots.txt`/`llms.txt`, returns a populated `PostHookReport`, and creates exactly one git commit per call. | `git log` in the tmp sandbox shows one `auto: post-hook iteration` commit after the test. | The Guardrail-most-visible-in-demo (the auto post-hook chain) runs deterministically. |
| 6 | Checkpoints | `tests/services/test_checkpoints.py` | `uv run pytest tests/services/test_checkpoints.py` | All 5 named checkpoints pass on a hand-built good fixture and fail on a hand-built bad fixture; `criteria_results` keys exactly match the spec table (no drift). | none | Every Checkpoint pillar instance is deterministic and reusable. |
| 7 | Orchestrator (MockWorker E2E) | `tests/services/test_orchestrator_mock.py` | `uv run pytest tests/services/test_orchestrator_mock.py` | A scripted 6-turn restaurant session (`ask_user(name) → ask_user(industry) → render_mockup → request_approval → write_file(bad html) → site_valid fails → write_file(fixed html) → site_valid passes → final`) drives every event type at least once, writes every named checkpoint row, exercises all 4 alarm types as raisable, and the crash-resume sub-test produces identical terminal state after a mid-loop kill + restart. | Inspect the tmp per-session DB and confirm `events` ordering by `id` matches insertion order. | All four rubric pillars + the Worker pillar are demonstrably wired on the MockWorker. **This is the major milestone before any LLM call.** |
| 8 | LLMWorker × 2 + LLM client + 429 fallback | `tests/services/test_llm_worker.py` | `uv run pytest tests/services/test_llm_worker.py` (offline) — `uv run pytest tests/services/test_llm_worker.py -m live` (one real chat + one real code call) | Offline stub coverage: envelope parse + one repair retry; 429-on-primary → success on fallback writes 2 `spend_log` rows with the second `is_fallback=1` and emits a `model_swapped` event; 429-on-both raises `tool_failed`; no-fallback-configured raises `tool_failed` immediately. Live: one real chat-stage and one real code-stage call return valid envelopes and write `spend_log` rows. | After the live run, `sqlite3 data/harness.db "select model, is_fallback, cost_usd from spend_log;"` shows the expected models. | The 429-swap path is provably correct without burning quota; the live test confirms the OpenRouter wiring under $0.05. |
| 9 | Domain bundle + FastAPI routes | `tests/api/test_web_api.py` | `uv run pytest tests/api/test_web_api.py` | `TestClient` drives the full route set with MockWorker injected: create session → resume → fetch detail (JSON shape exactly matches `docs/http-api.md`) → submit answer to a pending question → resume → final; `404` for unknown session id; `409` for resume on `completed`; `400` for bad answer body. | `curl` the running app for each route and diff against the doc examples. | The HTTP API matches `docs/http-api.md` exactly so the frontend can be built against it. |
| 10 | Jinja2 templates | `tests/api/test_web_ui.py` | `uv run pytest tests/api/test_web_ui.py` | `TestClient` renders each template (`index.html`, `session.html`, `awaiting.html`) for a representative session without raising; rendered HTML contains the expected anchors (event-row classes, checkpoint badges, alarm rows, model-swap badge when `fallback_count > 0`, cost total). | Browser walk-through of `index` → `session` → `awaiting` → submit answer; 2-second polling visibly updates the event timeline. | Every UI element promised by the demo flow is present in the templates. |
| 11 | Demo polish + Docker + HARNESS.md | `tests/test_demo_flow.py` | `uv run pytest tests/test_demo_flow.py -m "slow or not slow"` then `docker compose up` and a live demo run under $1 | Full MockWorker restaurant flow runs end-to-end and asserts: the engineered `<title>` failure fires `site_valid=fail` + an alarm + a follow-up turn that writes the fix + `site_valid=pass`; both `MODEL_CHAT` and `MODEL_CODE` models appear in `spend_log` during the live run; `docker compose down && up` preserves the session list (bind-mounted `data/` persists). | Watch the live demo from `docker compose up` to a `completed` session in the UI; total `spend_log` sum under $1. | `HARNESS.md` is shipped at repo root and `tests/test_demo_flow.py` is green. |
| 12 (stretch) | Polish | per-item tests, e.g. `tests/services/test_intent_audit.py`, `tests/api/test_ui_alarm_colors.py`, `tests/services/test_model_swap_demo.py` | `uv run pytest tests/services/test_intent_audit.py` (etc.) | Each polish item ships behind its own green test: the intent-audit checkpoint passes/fails on fixtures comparing a generated site against the seeded brief; alarm severity colors render in `session.html`; a live `MODEL_CODE` swap mid-session is visible in the UI and recorded in `spend_log`. | Visual check in the UI for color and swap badge. | Each polish item has a passing test before it is considered shipped. |

## Coverage notes

- **Pillar traceability.** Every rubric pillar has at least one gate that specifically asserts it: Guardrails (Steps 3, 4, 5), Checkpoints (Steps 6, 7, 11), Material (Steps 1, 7), Alarms (Steps 3, 7), Worker (Steps 8, 11).
- **Crash-resume.** Step 7's gate kills the orchestrator mid-loop and restarts; identical terminal state proves HTTP-triggered resume is just "call `run_until_pause` again."
- **No drift.** Step 9 asserts `GET /sessions/{id}` response keys exactly match the example in `docs/http-api.md` — the mechanism that keeps docs and code aligned.

## Out of scope for the test plan

Async / concurrency, multi-user / auth, Lighthouse / screenshot / visual-regression, sub-agent / verifier behavior — all deferred (see `docs/v1-spec.md`).
