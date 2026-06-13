# Building — Harness v1 Master Checklist

This file is the live state of the v1 build. It is self-sufficient: after `/clear`, loading **this file + `docs/resume.md` + the canonical plan** is enough to resume work without losing context. Update it after every step.

## Status snapshot

- **Last updated:** 2026-06-13
- **Current step:** Step 4 — Tools + dispatcher (next up)
- **Next step:** Step 5 — Validators + post-hooks
- **Last green test:** `uv run pytest tests/` (95 passed in 0.33s — Steps 0/1/2/3 combined).
- **Active blockers:** none

## The 12-step checklist

Each row is one step from the Build Order in `/Users/elroy/.claude/plans/ignore-any-timelines-they-re-tingly-flame.md`. A step is `done` only when its gate command is green and the "done when" criterion is true.

- [x] **Step 0 — Docs first.** Status: done (2026-06-13).
  - Gate: `uv run pytest --collect-only && uv sync`
  - Done when: `docs/v1-spec.md`, `docs/http-api.md`, `docs/test-plan.md`, `docs/building.md` are written; scoped edits applied to `implementation-architecture.md`, `overview.md`, `design.md`, `resume.md`, `skills/bootstrap.md`; `pyproject.toml`, `.env.example`, and empty layer folders are in place; `pytest --collect-only` and `uv sync` both succeed.
- [x] **Step 1 — UUID7 helper + DDL + Store.** Status: done (2026-06-13).
  - Gate: `uv run pytest tests/models/test_ids.py tests/services/test_store.py`
  - Done when: `new_id()` returns chronologically-ordered UUID7s; all 6 tables round-trip through `store.py`; FK enforcement asserted; `recent_spend_today_usd` correct across sessions and `is_fallback` rows.
- [x] **Step 2 — Envelope + Worker protocol + MockWorker.** Status: done (2026-06-13).
  - Gate: `uv run pytest tests/models/test_envelope.py tests/services/test_mock_worker.py`
  - Done when: envelope discriminated union parses each variant and rejects bad shapes; `MockWorker` pops scripted responses in order.
- [x] **Step 3 — Guardrails + Alarms.** Status: done (2026-06-13).
  - Gate: `uv run pytest tests/services/test_guardrails.py tests/services/test_alarms.py`
  - Done when: all four guardrail functions return correct booleans on the fixture matrix; each of the 4 alarm types persists with correct severity and emits an `alarm_raised` event.
- [ ] **Step 4 — Tools + dispatcher.** Status: pending.
  - Gate: `uv run pytest tests/services/test_tools.py`
  - Done when: each of the 6 tools tested on happy + failure path; non-allow-list tool denied + alarm raised; `write_file` rejects sandbox escapes.
- [ ] **Step 5 — Validators + post-hooks.** Status: pending.
  - Gate: `uv run pytest tests/services/test_validators.py tests/services/test_post_hooks.py`
  - Done when: known-bad HTML/CSS fail with specific error keys; known-good pass; `post_hooks.run` writes the 3 SEO files and creates exactly one git commit.
- [ ] **Step 6 — Checkpoints.** Status: pending.
  - Gate: `uv run pytest tests/services/test_checkpoints.py`
  - Done when: all 5 checkpoints pass on a good fixture, fail on a bad fixture, and `criteria_results` keys match the spec table.
- [ ] **Step 7 — Orchestrator (MockWorker E2E).** Status: pending.
  - Gate: `uv run pytest tests/services/test_orchestrator_mock.py`
  - Done when: scripted 6-turn restaurant session drives every event type, writes every named checkpoint row, and all 4 alarms are raisable; crash-resume sub-test produces identical terminal state. **Major milestone — all four pillars + Worker pillar demonstrable on MockWorker.**
- [ ] **Step 8 — LLMWorker × 2 + LLM client + 429 fallback.** Status: pending.
  - Gate: `uv run pytest tests/services/test_llm_worker.py` (offline) and `-m live` for the two real-call assertions.
  - Done when: envelope-repair retry covered; 429→fallback success writes `is_fallback=1` and emits `model_swapped`; 429→both-fail raises `tool_failed`; no-fallback-configured raises `tool_failed`; live chat + code calls return valid envelopes under $0.05.
- [ ] **Step 9 — Domain bundle + FastAPI routes.** Status: pending.
  - Gate: `uv run pytest tests/api/test_web_api.py`
  - Done when: every route in `docs/http-api.md` is implemented, returns the documented JSON shape, and the create→resume→detail→answer→resume→final flow passes end-to-end with MockWorker injected.
- [ ] **Step 10 — Jinja2 templates.** Status: pending.
  - Gate: `uv run pytest tests/api/test_web_ui.py`
  - Done when: each template renders without exception; rendered HTML contains the expected anchors; 2-second polling visibly updates the event timeline in the browser walk-through.
- [ ] **Step 11 — Demo polish + Docker + HARNESS.md.** Status: pending.
  - Gate: `uv run pytest tests/test_demo_flow.py` then `docker compose up` for a live demo under $1.
  - Done when: full restaurant flow runs end-to-end on MockWorker; live demo completes under $1; both `MODEL_CHAT` and `MODEL_CODE` appear in `spend_log`; `docker compose down && up` preserves the session list; `HARNESS.md` shipped at repo root.
- [ ] **Step 12 — Polish (stretch).** Status: pending.
  - Gate: per-item tests under `tests/services/` and `tests/api/`.
  - Done when: each polish item (intent-audit checkpoint, alarm severity colors, live `MODEL_CODE` swap demo) ships behind its own green test.

## How to resume after `/clear`

1. **Load these files first, in order:** `docs/resume.md`, `docs/building.md` (this file), `docs/v1-spec.md`, `/Users/elroy/.claude/plans/ignore-any-timelines-they-re-tingly-flame.md`. Re-read the plan section for the current/next step.
2. **Verify state** (parallelizable Bash calls): `pytest --collect-only`, `git log --oneline -10`, `uv sync`.
3. **How to know what's done:** the checklist below is authoritative; ticked boxes (`- [x]`) are done. Cross-check with `git log --oneline | grep "Step "` (commit convention: `Step N — <name>`). If they disagree, **trust git log** and fix the checklist.
4. **How to know what's next:** "Current step" / "Next step" at the top, then re-read the relevant step in the plan for the full spec.
5. **First action after resume:** re-run the gate for the most recently ticked step to confirm it's still green; fix any regression before starting new work.

## Decision log

Running list of design decisions made during the build. Each entry is one line plus a `Why:` line. Append as new non-obvious decisions are made.

- **2026-06-13 — Two-worker stage-mapped setup chosen** (DeepSeek chat / Qwen code) over a single worker. **Why:** split is free for code-gen (Qwen3-Coder:free at 200 req/day), keeps the $1 spend headroom, and makes the rubric Worker-pillar bonus permanently on without any extra ceremony.
- **2026-06-13 — `html5lib` + `tinycss2` for site validation** chosen over external CLI linters. **Why:** pure-Python so no shell dependency, runs inside the orchestrator process, and JS validation is explicitly deferred in v1 (no good pure-Python option).
- **2026-06-13 — Bottom-up build order** (data → service → API → frontend). **Why:** each layer is testable in isolation before the next is started; upper layers consume stable interfaces; MockWorker carries the orchestrator through Step 7 so the loop is provably correct before any LLM call.
- **2026-06-13 — UUID7 (TEXT) for all `id` columns.** **Why:** time-ordered (first 48 bits are ms timestamp) so ordering by `id` gives chronological order, no `AUTOINCREMENT`, single `new_id()` helper everywhere, clean.
- **2026-06-13 — FK columns on `events` (`material_id`, `checkpoint_id`, `alarm_id`) and a back-link on `alarms` (`triggered_by_event_id`, unenforced).** **Why:** joining `events` to any of the three referenced tables gives a queryable trace; the `alarms→events` back-pointer would create a cycle with the FK from `events.alarm_id`, so it's a logical reference only.
- **2026-06-13 — 429 → auto-swap to fallback paid model, log `is_fallback=1`, append `model_swapped` event.** **Why:** keeps the demo running without manual intervention; makes the swap visible in the UI and queryable in `spend_log`; bounded blast radius (single retry, then `tool_failed` alarm).

## Maintenance protocol

After every completed step:

1. **Update the status snapshot** at the top of this file: bump "Last updated", advance "Current step" / "Next step", set "Last green test" to the just-passed gate command, clear or update "Active blockers".
2. **Tick the checkbox** for the completed step (`- [ ]` → `- [x]`) and set its status to `done`.
3. **Run the gate command one more time** to confirm green, then commit with message `Step N — <name>` (no co-authors, per global CLAUDE.md).
4. **Append to the decision log** if a non-obvious decision was made during the step (one line + a `Why:` line). Skip if the step was purely mechanical.
5. **If the step changed any doc**, update `docs/resume.md` and `docs/v1-spec.md` to match, per the per-step `*Doc:*` notes in the plan.
6. **If a new blocker emerged**, write it into "Active blockers" with enough detail that a future-self after `/clear` can act on it.

The discipline of updating this file on every step is what makes context clears safe. Skipping the update is the only way the build gets lost.
