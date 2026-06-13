# harness

A durable, resumable agent harness built for the 24-hour Build Challenge. The demo domain is a **website builder for non-technical small-business owners**: a chat-driven flow that collects a business brief, gets mockup approval, and iteratively generates an SEO-optimized static site.

Built on the four-pillar model — **Loop · Tools · Guardrails · Observability** — with a swappable Worker behind a data-only protocol so the LLM is one moving part among many.

## Quick start

```bash
# 1. Install uv if you don't have it
brew install uv

# 2. Install dependencies (creates .venv)
uv sync

# 3. Configure
cp .env.example .env
# edit .env: set OPENROUTER_API_KEY

# 4. Run the server
uv run uvicorn harness.api.app:app --reload --port 8000
```

Open http://localhost:8000.

Sessions persist under `data/sessions/{uuid}.db`; generated sites under `data/sites/`. Both directories are created on first run.

## Configuration

All configuration is via `.env` (gitignored). See `.env.example` for the full template.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes (for live runs) | — | OpenRouter API key. Get one at https://openrouter.ai/keys. Not needed if you only run the offline test suite. |
| `MODEL_CHAT` | yes | `deepseek/deepseek-v4-flash:free` | Worker used for bootstrap, brief, mockup, and clarification stages. |
| `MODEL_CHAT_FALLBACK` | no | `deepseek/deepseek-v4-flash` | Paid fallback used when `MODEL_CHAT` returns 429. Logged with `is_fallback=1`. |
| `MODEL_CODE` | yes | `qwen/qwen3-coder:free` | Worker used for `write_file` stages (HTML / CSS / JS generation). |
| `MODEL_CODE_FALLBACK` | no | — | Paid fallback for `MODEL_CODE`. |
| `SPEND_CAP_USD` | no | `1.0` | Daily spend ceiling. When tripped, the orchestrator pauses with `paused_reason="spend_cap"` and requires human approval to continue. |
| `TURN_CAP` | no | `10` | Iterations between human-approval gates. Tripped → `paused_reason="awaiting_human"`. |

## User flows

The UI is server-rendered Jinja2 with a small amount of JS for the chat input. All state lives in SQLite; nothing is in-memory.

### Create a session

`POST /sessions` (from the **New session** form on `/`) creates an empty session at `current_stage="bootstrap"`. No materials are pre-populated — the worker collects everything via `ask_user`.

### Bootstrap → Brief → Mockup → Build

The orchestrator walks the worker through these stages, pausing for human input at each gate:

1. **Bootstrap.** Chat worker uses `ask_user` to collect the **Business Brief** (name, what you sell, who you sell to, tone, contact info). Each question pauses the session at `status="awaiting_human"`; the user types an answer in the chat input.
2. **Brief approval.** Worker calls `request_approval(subject="business_brief", details=…)`. The user clicks **Approve** or **Deny**.
3. **Mockup.** Worker calls `render_mockup` and then `request_approval(subject="mockup", …)` with an ASCII layout. User approves or sends feedback.
4. **Build (loop).** Code worker generates HTML/CSS/JS via `write_file`. After every write, the post-hook chain runs: `validate_site` → `regenerate_seo_artifacts` (writes `sitemap.xml`, `robots.txt`, `llms.txt`) → auto `git commit`.
5. **Intent audit.** Before finishing, the worker self-checks the generated site against the brief.
6. **Final.** Worker emits a `final` envelope; the orchestrator marks the session `completed`.

### Resume / answer / rewind

The session-detail page exposes three controls:

- **Resume** (`POST /sessions/{id}/resume`) — drive the orchestrator until it pauses again. Auto-clears `continuation_approval` pendings (the iter-cap and spend-cap gates) so the user's mental model is "Resume = continue past whatever safety cap is blocking me." Content gates (brief / mockup / freeform `ask_user`) still require an explicit answer.
- **Answer / Approve / Deny** (`POST /sessions/{id}/answer`) — submit a response to a pending question or approval. Re-enters the loop afterward.
- **Rewind** (`POST /sessions/{id}/rewind`) — destructive but tracked: delete everything after a chosen `awaiting_human` event, re-pend the original question, and let the user answer differently. `spend_log` is intentionally preserved (real cost is real).

### Observability

Every page polls nothing — refresh manually. The session detail view shows:

- **Events** — the append-only log, the source of truth.
- **Checkpoints** — five named gates (`brief_complete`, `mockup_approved`, `site_valid`, `seo_artifacts_present`, `intent_match`) with `criteria_results`.
- **Alarms** — four types (`iteration_limit_reached`, `spend_cap_reached`, `tool_failed`, `output_schema_violation`, …) with severity and recommended action.
- **Spend summary** — total USD, per-model breakdown, fallback count.
- **LLM calls** — last 50 API attempts with full request/response payloads.

## Testing

```bash
# Offline suite (no API key needed) — last green: 215 passed
uv run pytest tests/ -m "not live"

# Type check
uv run pyright harness/ tests/

# Live smoke tests (requires OPENROUTER_API_KEY, costs < $0.05)
uv run pytest tests/ -m live
```

## Docs

| File | Purpose |
|---|---|
| `docs/resume.md` | Load first after `/clear` — restores full project context. |
| `docs/building.md` | Live build status, 12-step checklist, decision log. |
| `docs/v1-spec.md` | Source of truth: data model, checkpoints, alarms, tools. |
| `docs/http-api.md` | Full HTTP route reference. |
| `docs/overview.md` | <500-word architectural overview. |
| `docs/design.md` | Mermaid component + flow diagrams. |

## Project status

The v1 build is at **Step 10 of 12** — all four pillars are demonstrable end-to-end on both MockWorker and live OpenRouter. Step 11 (Docker packaging, demo polish, `HARNESS.md`) is next. See `docs/building.md` for the live checklist.
