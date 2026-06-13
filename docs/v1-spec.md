# v1 Spec — Harness + Agent MVP

## 1. Purpose

This document is the **source of truth** for the v1 Harness MVP. It defines the file structure, data model, envelopes, event/material/checkpoint/alarm sets, tools, post-hooks, worker setup, loop semantics, integration points, and demo flow we will build against. If code disagrees with this doc, **fix the code or update this doc** — drift is not permitted. Anything not listed here is out of scope for v1. Architecture-level decisions live in `docs/implementation-architecture.md`; constraints we cannot edit live in `docs/harness.md`; this spec is the narrowed, dated commitment for what ships.

## 2. Scope

### In scope (v1)
- Single-process, sync, HTTP-triggered loop (`run_until_pause`).
- Two SQLite databases: core (`data/harness.db`) + per-session (`data/sessions/{uuid}.db`).
- Pydantic-typed worker envelope (`Message`, `WorkerContext`, `ToolCall`/`Final`/`Escalate`).
- Two `LLMWorker` instances (chat + code) over OpenRouter, env-configurable, with **free→paid 429 auto-swap**.
- `MockWorker` for tests and dev iteration.
- 6 worker-callable tools: `ask_user`, `request_approval`, `render_mockup`, `read_file`, `write_file`, `list_files`.
- **Auto post-hook chain** after every successful `write_file`: validate → regen SEO → git commit.
- 5 deterministic checkpoints: `business_brief_confirmed`, `mockup_renders`, `mockup_approved`, `site_valid`, `seo_artifacts_present`.
- 4 named alarms: `iteration_limit_reached`, `spend_cap_reached`, `output_schema_violation`, `tool_failed`.
- Declared guardrails: tool allow-list, sandbox path, 10-iter human-approval gate, $1/day spend cap, output JSON envelope schema.
- HITL pause/resume via `ask_user` / `request_approval` and `POST /sessions/{id}/answer`.
- Jinja2 server-rendered chat-first web UI (session list + chat-style session detail with conversation projection, context-sensitive input area for pending materials, details accordion for events/checkpoints/models, iframe of generated site). No polling — explicit Refresh + post-action reload.
- Docker deploy (deferred to Step 11; macOS dev uses uvicorn directly).
- Restaurant persona (Maria) is the canonical demo and integration test.

### Out of scope (deferred — architectural source in parentheses)
- **Sub-agents** (`docs/implementation-architecture.md`) — adds orchestration surface without rubric value for v1.
- **Verifier sub-agent / Tier-2 checkpoints** (`docs/implementation-architecture.md`) — non-deterministic gate; v1 only ships deterministic Tier-1.
- **Context compaction** (`docs/implementation-architecture.md`) — not needed within a single demo session.
- **Intent-audit checkpoint** (`docs/implementation-architecture.md`) — semantic compare of finished site vs. brief; deferred to optional Step 12.
- **Lighthouse / screenshot tools** (`docs/implementation-architecture.md`) — heavyweight, no rubric coverage.
- **Persona variants beyond Maria** (`docs/user-stories.md`) — Maria covers the full pillar surface.
- **Advanced replay UI**, **production hosting deploy**, **users table / multi-tenant** — see "Out of scope" in the plan.

## 3. File structure (by layer)

Each top-level subfolder under `harness/` is one layer. Files never reach across layers except via documented Integration Points (§14). Upper layers may import from lower; the reverse is forbidden.

```
harness/
  __init__.py
  models/                       # Layer 1 — types & schema (no logic)
    __init__.py
    ids.py                      # new_id() -> str (UUID7 via uuid_utils)
    enums.py                    # EventType, MaterialType, AlarmType, CheckpointName, Stage, Severity, Status
    envelope.py                 # Pydantic: Message, WorkerContext, ToolCall/Final/Escalate
    ddl.py                      # CREATE TABLE strings (FK-safe order) + PRAGMA setup
  services/                     # Layer 2 — business logic (no HTTP)
    __init__.py
    store.py                    # all SQL; opens both DBs; sets PRAGMA foreign_keys=ON
    worker.py                   # Worker Protocol + MockWorker
    guardrails.py               # pure functions: allow-list, sandbox, turn cap, spend cap
    alarms.py                   # raise_alarm() — persists + appends event
    tools/
      __init__.py               # ToolContext, ToolResult, dispatch()
      user.py                   # ask_user, request_approval
      files.py                  # read_file, write_file, list_files (sandboxed)
      mockup.py                 # render_mockup
    validators.py               # html5lib / tinycss2 / xml.etree pure functions
    post_hooks.py               # validate → SEO regen → git commit chain
    checkpoints.py              # registry + 5 evaluator functions
    orchestrator.py             # run_until_pause(session_id, worker_for_stage)
    llm.py                      # OpenRouter HTTP + spend logging + 429 detection
    llm_worker.py               # LLMWorker(primary, fallback) — auto-swap on 429
  domain/                       # Layer 3 — domain bundle (declarative config)
    __init__.py
    website_builder.py          # system prompt, allowed tools, checkpoint set, stage→worker map, seed brief
  api/                          # Layer 3 — HTTP surface (thin, delegates to services)
    __init__.py
    app.py                      # FastAPI app + routes
    dependencies.py             # FastAPI deps (store handle, domain bundle)
    view_helpers.py             # pure functions: format_time_hms, format_event_for_table,
                                # build_conversation, derive_active_models
  templates/                    # Layer 4 — frontend
    _base.html                  # global layout + chat CSS
    index.html                  # session list + new-session form
    session.html                # chat-first detail page (header + main partial + JS)
    _session_main.html          # chat log + input area + Details accordion + cost/alarms
                                # (also rendered alone when ?partial=1 is set)
data/
  harness.db                    # core DB
  sessions/{uuid7}.db           # per-session DB
  sites/{uuid7}/                # generated files + local git repo
tests/                          # mirrors harness/ structure
  conftest.py                   # tmp-path DB + sandbox fixtures
  models/
    test_ids.py                 # Step 0 gate
    test_envelope.py            # Step 2 gate
  services/
    test_store.py               # Step 1 gate
    test_mock_worker.py         # Step 2 gate
    test_guardrails.py          # Step 3 gate
    test_alarms.py              # Step 3 gate
    test_tools.py               # Step 4 gate
    test_validators.py          # Step 5 gate
    test_post_hooks.py          # Step 5 gate
    test_checkpoints.py         # Step 6 gate
    test_orchestrator_mock.py   # Step 7 gate — full mock-driven session
    test_llm_worker.py          # Step 8 gate — includes 429→fallback test
  api/
    test_web_api.py             # Step 9 gate (FastAPI TestClient)
    test_web_ui.py              # Step 10 gate (template rendering)
  test_demo_flow.py             # Step 11 gate (full restaurant flow, top-level)
.env / .env.example             # MODEL_CHAT, MODEL_CHAT_FALLBACK, MODEL_CODE, MODEL_CODE_FALLBACK, OPENROUTER_API_KEY
pyproject.toml                  # uv-managed; min release age 7d
Dockerfile / docker-compose.yml
```

**Dependencies** (`pyproject.toml`): `pydantic`, `html5lib`, `tinycss2`, `uuid_utils`, `httpx`, `jinja2`, `fastapi`, `uvicorn[standard]`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`. Configured for `uv` with `tool.uv.min-release-age = "7d"`.

## 4. Data model

Two SQLite databases, 6 tables total. No `users` table (single-user local app). No `run_meta` (guardrails declared in code).

### UUID7 ids
Every `id` column is a **UUID7 stored as TEXT** (full formatted string, e.g. `0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d`). UUID7 is time-ordered — the first 48 bits are millisecond timestamp — so ordering by `id` yields chronological order and gives monotonic insertion without `AUTOINCREMENT`. The `uuid_utils` package is used; `harness/models/ids.py` exposes a single `new_id() -> str` helper that wraps `uuid_utils.uuid7()` and stringifies the result. All inserts go through `new_id()` — ids are never hand-written.

### FK semantics
Foreign keys are **enforced within each SQLite DB**. `store.py` sets `PRAGMA foreign_keys = ON` on every connection. Cross-database relationships (per-session-DB → core-DB) cannot use FK and are documented as logical references only. `spend_log.session_id` → `sessions.id` is same-DB (both in core).

### Core DB — `data/harness.db`

```sql
CREATE TABLE sessions (
  id                  TEXT PRIMARY KEY,           -- UUID7
  status              TEXT NOT NULL,              -- 'active' | 'awaiting_human' | 'completed' | 'failed'
  current_stage       TEXT NOT NULL,              -- 'bootstrap' | 'mockup' | 'build' | 'done'
  iter_since_approval INTEGER NOT NULL DEFAULT 0, -- resets when human approves
  created_at          TEXT NOT NULL,              -- ISO8601
  updated_at          TEXT NOT NULL
);

CREATE TABLE spend_log (
  id          TEXT PRIMARY KEY,                   -- UUID7
  ts          TEXT NOT NULL,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  model       TEXT NOT NULL,                      -- exact model string used for this call
  is_fallback INTEGER NOT NULL DEFAULT 0,         -- 1 if call used the fallback (paid) model after a 429
  tokens_in   INTEGER NOT NULL,
  tokens_out  INTEGER NOT NULL,
  cost_usd    REAL NOT NULL                       -- 0.0 for :free models
);
CREATE INDEX idx_spend_day ON spend_log(ts);      -- for $1/day rollup query
CREATE INDEX idx_spend_session ON spend_log(session_id);
```

### Per-session DB — `data/sessions/{session_id}.db`

Tables must be created in this order so REFERENCES targets exist: **(1) material → (2) checkpoints → (3) alarms → (4) events**. `events` points to all three; `alarms.triggered_by_event_id` is logical only (a hard FK would cycle with `events.alarm_id`).

```sql
CREATE TABLE material (
  id         TEXT PRIMARY KEY,                    -- UUID7
  direction  TEXT NOT NULL,                       -- 'in' | 'out'
  stage      TEXT NOT NULL,
  type       TEXT NOT NULL,                       -- see Material types below
  content    TEXT NOT NULL,                       -- JSON
  pending    INTEGER NOT NULL DEFAULT 0,          -- 1 = awaiting human response
  created_at TEXT NOT NULL
);

CREATE TABLE checkpoints (
  id               TEXT PRIMARY KEY,              -- UUID7
  name             TEXT NOT NULL,                 -- one of the 5 named checkpoints
  stage            TEXT NOT NULL,
  status           TEXT NOT NULL,                 -- 'pass' | 'fail'
  criteria_results TEXT NOT NULL,                 -- JSON {criterion_name: bool}
  material_id      TEXT NULL REFERENCES material(id),  -- the material this evaluated
  created_at       TEXT NOT NULL
);
CREATE INDEX idx_ckpt_name_created ON checkpoints(name, created_at);
CREATE INDEX idx_ckpt_material    ON checkpoints(material_id);

CREATE TABLE alarms (
  id                    TEXT PRIMARY KEY,         -- UUID7
  type                  TEXT NOT NULL,            -- one of the 4 named alarm types
  severity              TEXT NOT NULL,            -- 'warning' | 'error' | 'critical'
  context               TEXT NOT NULL,            -- JSON, type-specific shape
  recommended_action    TEXT NOT NULL,
  stage                 TEXT NOT NULL,
  triggered_by_event_id TEXT NULL,                -- logical FK to events.id (set after event insert); NOT enforced to avoid circularity
  resolved              INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL
);
CREATE INDEX idx_alarms_event ON alarms(triggered_by_event_id);

CREATE TABLE events (
  id            TEXT PRIMARY KEY,                 -- UUID7; order-by-id == chronological
  ts            TEXT NOT NULL,
  type          TEXT NOT NULL,                    -- see Event types below
  stage         TEXT NOT NULL,
  payload       TEXT NOT NULL,                    -- JSON blob, shape depends on type
  material_id   TEXT NULL REFERENCES material(id),
  checkpoint_id TEXT NULL REFERENCES checkpoints(id),
  alarm_id      TEXT NULL REFERENCES alarms(id)
);
CREATE INDEX idx_events_material   ON events(material_id);
CREATE INDEX idx_events_checkpoint ON events(checkpoint_id);
CREATE INDEX idx_events_alarm      ON events(alarm_id);
```

Every event row optionally points to the material, checkpoint, or alarm it concerns (zero or one of the three — never multiple). Joining `events` to any of the three tables produces a queryable trace. `alarms.triggered_by_event_id` is set after the alarm-raising event is appended.

## 5. Pydantic envelope (`harness/models/envelope.py`)

```python
class Message(BaseModel):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str
    tool_call_id: str | None = None          # set when role='tool'

class WorkerContext(BaseModel):
    session_id: str
    turn: int                                 # absolute turn number, 1-indexed
    stage: str                                # routes to chat or code worker
    system_prompt: str                        # includes bootstrap skill + business brief if set
    messages: list[Message]
    tool_schemas: list[dict]                  # JSON Schema for each allowed tool
    state: dict                               # {last_checkpoint, last_alarm, brief, sandbox_path}

class ToolCall(BaseModel):
    type: Literal['tool_call'] = 'tool_call'
    tool: str
    args: dict

class Final(BaseModel):
    type: Literal['final'] = 'final'
    summary: str

class Escalate(BaseModel):
    type: Literal['escalate'] = 'escalate'
    reason: str

WorkerResponse = Annotated[ToolCall | Final | Escalate, Field(discriminator='type')]
```

## 6. Event types (closed set)

| `type` | Emitted by | `payload` shape |
|---|---|---|
| `worker_input` | orchestrator before each `worker.act()` | `{model, messages_count, tokens_estimate}` |
| `worker_output` | orchestrator after each `worker.act()` | the raw `WorkerResponse` JSON + `{model, is_fallback, tokens_in, tokens_out, cost_usd}` |
| `model_swapped` | LLMWorker after a 429-driven swap | `{from, to, reason}` |
| `tool_call` | tool dispatcher | `{tool, args, allowed: bool}` |
| `tool_result` | tool dispatcher | `{tool, ok: bool, result_or_error}` (errors are data, not exceptions) |
| `post_hook_run` | orchestrator after `write_file` | `{validate_ok, seo_regenerated, git_commit_sha}` |
| `checkpoint_result` | orchestrator after checkpoint eval | `{name, status, criteria_results}` |
| `alarm_raised` | guardrails / checkpoints / dispatcher | `{alarm_id, type, severity}` |
| `awaiting_human` | orchestrator on ask_user/request_approval/escalate | `{material_id, reason}` |
| `human_resumed` | `/answer` HTTP handler | `{material_id, answer_or_decision}` |

## 7. Material types (closed set)

| `type` | `direction` | When created | `content` shape |
|---|---|---|---|
| `business_brief` | out | bootstrap complete | the brief dict (industry, name, contact, pages, palette, ...) |
| `pending_question` | out | `ask_user` or `request_approval` invoked | `{question, options?}` |
| `user_answer` | in | `/answer` POST | `{answer_text}` |
| `user_approval` | in | `/answer` POST for approval | `{approved: bool, notes?}` |
| `layout_spec` | out | worker chose mockup layout | `{sections: [...], primary_cta}` |
| `mockup` | out | `render_mockup` returned | `{ascii: str, regions: [...]}` |
| `site_file` | out | `write_file` succeeded | `{path, content_hash, bytes}` |
| `validation_result` | out | post-hook validators ran | `{html: {...}, seo: {...}}` |

## 8. The 5 checkpoints

All are **deterministic** functions over the latest relevant material. `status='pass'` iff every criterion is `true`. Each evaluates after a specific trigger event.

| Name | Evaluated when | Criteria dict | Alarms on fail |
|---|---|---|---|
| `business_brief_confirmed` | `user_approval` material arrives for the brief | `{brief_persisted: bool, user_approved: bool}` | `output_schema_violation` (if brief malformed), `iteration_limit_reached` (if approval loops) |
| `mockup_renders` | `render_mockup` tool returns | `{ascii_non_empty: bool, all_regions_present: bool, declared_sections_covered: bool}` | `tool_failed`, `iteration_limit_reached` |
| `mockup_approved` | `user_approval` material arrives for the mockup | `{user_approved: bool}` | `iteration_limit_reached` |
| `site_valid` | post-hook validate completes | `{html5_parses: bool, html5_errors: list[str], css_parses: bool, css_errors: list[str], has_title: bool, has_meta_viewport: bool, has_lang: bool, has_h1: bool}` | `tool_failed`, `iteration_limit_reached` |
| `seo_artifacts_present` | post-hook SEO regen completes | `{sitemap_xml_valid: bool, robots_txt_present: bool, llms_txt_present: bool}` | `tool_failed` |

Note: `site_valid` uses `html5lib` (strict parse + tag-presence walk) and `tinycss2` (parse + error list) from `validators.py`.

## 9. The 4 alarms

Each alarm row: `{id, type, severity, context, recommended_action, stage, triggered_by_event_id, resolved, created_at}`.

| Type | Severity (default) | Context shape | Recommended-action template |
|---|---|---|---|
| `iteration_limit_reached` | `warning` | `{iter_count, last_checkpoint, stage}` | "Pause for human approval; agent has iterated {iter_count} times without approval at stage {stage}." |
| `spend_cap_reached` | `critical` | `{spent_usd, cap_usd, window: 'day'}` | "Halt all worker calls; spent ${spent_usd} of ${cap_usd} cap for window '{window}'. Wait for window reset or raise cap." |
| `output_schema_violation` | `critical` | `{parse_error, repair_attempt, raw_text_preview}` | "Repair attempted {repair_attempt}x and still invalid. Stop and surface raw_text_preview to human." |
| `tool_failed` | `error` | `{tool, args, error_kind, error_message}` | "Tool {tool} failed with {error_kind}. Inspect args; consider retry or alternative tool." |

`iteration_limit_reached` and `spend_cap_reached` are state-based and auto-resolve when the condition no longer holds (checked at the start of `run_until_pause`). `tool_failed` and `output_schema_violation` are event-based and stay `resolved=0` until explicitly resolved.

## 10. The 6 tools

All six are worker-callable. `ask_user` and `request_approval` are escalation surfaces (write `pending_question` material + return paused sentinel). `write_file` triggers the post-hook chain. SEO regen, validation, and git commit are **not** in the tool list — they are post-hooks (§11).

| Tool | Args shape | Returns shape | Sandboxed? | Triggers post-hooks? |
|---|---|---|---|---|
| `ask_user` | `{question: str, options?: list[str]}` | `ToolResult(ok=True, result={paused: True, material_id})` | n/a | no |
| `request_approval` | `{summary: str, payload: dict}` | `ToolResult(ok=True, result={paused: True, material_id})` | n/a | no |
| `render_mockup` | `{layout_spec: dict}` | `ToolResult(ok=True, result={ascii: str, regions: list})` | n/a | no |
| `read_file` | `{path: str}` | `ToolResult(ok=True, result={content: str})` | yes (sandbox_path) | no |
| `write_file` | `{path: str, content: str}` | `ToolResult(ok=True, result={path, bytes})` | yes (sandbox_path) | **yes** |
| `list_files` | `{path?: str}` | `ToolResult(ok=True, result={entries: list[str]})` | yes (sandbox_path) | no |

All tools return `ToolResult(ok=bool, result, error)`. Errors are **data**, never exceptions. Disallowed tools (per allow-list) return `ok=False` and the dispatcher raises a `tool_failed` alarm.

## 11. Auto post-hook chain

**Not a worker tool.** Runs automatically after every successful `write_file` into the sandbox. The worker cannot opt out, cannot invoke it directly, and cannot see it in the tool list. This is the centerpiece of the Guardrails demo moment ("the harness keeps the agent honest").

**Chain order** (in `harness/services/post_hooks.py::run(sandbox_path) -> PostHookReport`):

1. **Validate** — `validate_html` + `validate_css` over all changed files; persist a `validation_result` material.
2. **Regenerate SEO** — write `sitemap.xml`, `robots.txt`, `llms.txt` from the current sandbox state.
3. **Git commit** — `git add . && git commit -m "auto: post-hook iteration"` in the sandbox's local repo.

**Failure handling:** the chain **always completes** (later steps still run even if earlier ones fail). Each failure raises a `tool_failed` alarm with `error_kind` set (`html_invalid`, `css_invalid`, `seo_write_failed`, `git_commit_failed`). The final `post_hook_run` event captures `{validate_ok, seo_regenerated, git_commit_sha}` (sha=None if commit failed). Checkpoints `site_valid` and `seo_artifacts_present` are then evaluated against the resulting state.

## 12. Two-worker setup + 429 fallback

### Env vars and defaults

| Env var | Default | Purpose |
|---|---|---|
| `MODEL_CHAT` | `deepseek/deepseek-v4-flash:free` | Primary chat model |
| `MODEL_CHAT_FALLBACK` | `deepseek/deepseek-v4-flash` | Paid variant; used after 429 |
| `MODEL_CODE` | `qwen/qwen3-coder:free` | Primary code model (20 req/min, 200 req/day) |
| `MODEL_CODE_FALLBACK` | *(empty)* | Set to a paid coder when configured |
| `OPENROUTER_API_KEY` | *(required)* | OpenRouter auth |

### Stage → worker map (in `harness/domain/website_builder.py`)

| Stage | Worker |
|---|---|
| `bootstrap` | chat |
| `mockup` | chat |
| `build` | code |
| `done` | n/a (terminal) |

Both workers implement the `Worker` Protocol via `LLMWorker(primary, fallback, llm_client, event_sink)`. Both share `services/llm.py` and log spend per call to core `spend_log` with the exact model string used and an `is_fallback` flag.

### 429 → fallback flow

1. `services/llm.py` raises a typed `RateLimited(model)` on HTTP 429.
2. `LLMWorker.act()` catches it. If `fallback` is set and non-empty, retry the same request once with `fallback`, logging the call to `spend_log` with `is_fallback=1` and the fallback model string.
3. The orchestrator (via `event_sink`) appends a `model_swapped` event (payload: `{from, to, reason: 'rate_limited'}`) so the swap is visible in the UI.
4. If the fallback also returns 429, or `fallback` is empty/unset, raise a `tool_failed` alarm with `context.error_kind='rate_limited'` and let the orchestrator pause for human approval.

### Visibility

`spend_log.model` and `spend_log.is_fallback` plus `events.payload` for `worker_input`/`worker_output` carry the per-call model. The UI's session detail surfaces both the active model and a "swapped" badge whenever any `is_fallback=1` row appears.

## 13. The loop & pause/resume model

**Sync, single-process, HTTP-triggered.** `POST /sessions/{id}/resume` calls `orchestrator.run_until_pause(session_id)`. It runs turns until it hits a wall:

- `final` envelope → session `completed`.
- `escalate` envelope OR `ask_user` / `request_approval` tool → session `awaiting_human`.
- Spend cap hit OR 10-iter human-approval gate hit → session `awaiting_human` + alarm.
- Crash → state survives in the event log; the next `/resume` rebuilds.

State lives entirely in SQLite. `ask_user` and `request_approval` are surfaced as tools; they record a `pending_question` material with `pending=1`, set `awaiting_human`, and the loop exits cleanly via a sentinel `ToolResult`.

On resume after the user submits via `POST /sessions/{id}/answer`, the answer is written as `user_answer` / `user_approval` material and fed back to the worker **as the tool result in message history** — the worker just sees `ask_user(...) -> "answer"` in the next turn's `messages`. The orchestrator does not branch on whether the worker is real or mock.

`POST /sessions/{id}/resume` carries explicit unstick semantics: if the session is `awaiting_human`, the route calls `orchestrator.force_continue(session_id, ...)` BEFORE invoking `run_until_pause`. `force_continue` auto-approves any pending `continuation_approval` materials (persisting a `user_approval` row with `auto_approved_via_resume: True`, appending a `human_resumed` event, marking the pending resolved), resets `iter_since_approval` to 0, and flips status back to `active` so the loop can run. Real content gates (`approval` on `business_brief`/`mockup`, freeform `ask_user`) are left pending — those still require an explicit `/answer`. The user's mental model: "/resume means continue past whatever safety cap is blocking me; real questions still need answers."

### Web UI (HITL surface)

The session detail page is **chat-first**. Layout, top to bottom:

1. **Compact header** — short session id, status badge, current-stage badge, Refresh link.
2. **Chat panel** — the conversation log (scrollable, min 60vh) plus a bottom-anchored input area.
3. **Details accordion** (`<details>`, collapsed by default) — active models, checkpoints, events table, site iframe.
4. **Footer cards** — cost summary, alarms (severity-coloured left border).

**Conversation projection** (`build_conversation` in `harness/api/view_helpers.py`): each session event becomes zero or one chat bubble.
- `human_resumed` → user bubble. The body depends on the answer's `kind`: `"approval"` renders "Approved/Denied {subject}"; `"continuation_approval"` renders "Approve continuation"/"Stop"; missing `kind` renders the literal `answer_text`.
- `worker_output` → agent bubble. `final` envelopes render the summary with a "final" tag; `escalate` envelopes render the reason with an "escalate" tag; `tool_call` envelopes use per-tool templates (`ask_user` → the question text, `request_approval` → a subject-aware approval card (see "Approval bubbles" below), `render_mockup` → `Rendered mockup ({N} sections)`, `write_file` / `read_file` / `list_files` → one-line file action summaries).

**Approval bubbles** (`request_approval`): the bubble body is a card rendered by `_render_approval_card` in `harness/api/view_helpers.py` — no raw JSON, no `<details>` expander is ever shown to the end user. `subject == 'business_brief'` renders a card with hoisted name + tagline headings and a `<dl class="brief-rows">` grid of known fields (industry, contact, hours, palette, pages, primary_cta, socials, …); palette dicts emit small inline `.swatch` color chips next to hex codes (only after the value matches `^#[0-9A-Fa-f]{3,8}$` — any other value renders as escaped text only); `hours` keys map to human day-range labels (`mon_thu` → `Mon–Thu`); a `contact` sub-dict is hoisted so `phone`/`email`/`address` appear as top-level rows; `socials` renders inline as `Instagram: @x, Twitter: @y`. `subject == 'mockup'` renders a compact heading + "Approve the layout above to proceed" prompt (the ASCII art lives in the prior `render_mockup` bubble, not here) plus an optional sections / primary CTA summary if `details` supplied them. Any other subject falls back to `Heading (subject title-cased) + labeled list` of every top-level key. All user-derived values are HTML-escaped via `html.escape` before concatenation — the card bypasses mistune and emits HTML directly.
- All other event types are skipped from the chat (they appear in the Details accordion's events table).

**Input area** (context-sensitive on `session.status` + the first pending material). The input area renders ONLY the action controls and a one-line muted cue — it does NOT restate the question / subject / details that the agent bubble immediately above already shows.
- `awaiting_human` + pending `approval` → Approve / Deny buttons + optional notes.
- `awaiting_human` + pending `continuation_approval` → Approve / **Stop** buttons + optional notes. Three pending kinds total (`approval`, `continuation_approval`, freeform).
- `awaiting_human` + pending freeform `ask_user` → option buttons (if `content.options` non-empty) plus an "Other…" reveal that surfaces a textarea BELOW the options-row (options stay visible and clickable; clicking a canned option still wins over typed text), OR a textarea + Send when no options.
- `active` → disabled textarea + Resume button.
- `completed` / `failed` → disabled textarea, no buttons.

**Markdown rendering**: agent bubbles whose body comes from the LLM — `final.summary`, `escalate.reason`, `ask_user.question`, `request_approval.subject` — are rendered server-side via [mistune](https://mistune.lepture.com) with `escape=True` so embedded HTML in untrusted LLM output is escaped (XSS-safe) and `hard_wrap=True` so newlines become `<br>`. Internal tool-call summary strings (e.g. `"Wrote index.html (1234 bytes)"`) and all user bubbles are HTML-escaped plain text — they are not markdown-rendered. Each conversation entry carries both `body` (raw text, for tests/debug) and `body_html` (the safe HTML the template emits).

**Events table** (inside Details): per-type human summaries generated by `format_event_for_table` (`harness/api/view_helpers.py`). Columns: HH:MM:SS time, type badge (colour-coded for severity / pass-fail), one-line summary. Capped at 200 most recent, newest at the bottom. The `model_swapped` row gets a tinted background so the rubric bonus moment stands out.

**Active models card**: reads `MODEL_CHAT`, `MODEL_CHAT_FALLBACK`, `MODEL_CODE`, `MODEL_CODE_FALLBACK` from env (`derive_active_models`). A "swapped this session" badge appears next to the chat-primary row when any `model_swapped` event has fired in the session.

The `awaiting.html` template was removed in this redesign — the pending-material form is now the chat input area, lives in `_session_main.html`, and ships in both full and `?partial=1` renders so the `view_session` route can return either.

## 14. Integration points

Strict module boundaries. Each line is the only contract between two modules.

1. **HTTP → orchestrator**: `POST /sessions/{id}/resume` → `orchestrator.run_until_pause(session_id) -> SessionStatus`. Synchronous; blocks until pause/terminal/cap.
2. **HTTP → store** (read paths): `GET /sessions[/{id}]` calls `store.load_session()`, `store.load_events()`, `store.load_alarms()`, etc. No business logic in handlers.
3. **Orchestrator → store**: `load_session`, `update_session_status`, `append_event`, `persist_material`, `persist_checkpoint`, `persist_alarm`, `recent_spend_today`.
4. **Orchestrator → worker**: `domain.worker_for_stage(session.current_stage).act(ctx: WorkerContext) -> WorkerResponse`. Orchestrator does not know which model is behind the worker.
5. **Worker → LLM client**: `llm.chat(model: str, messages: list[Message], response_format: dict) -> ChatResponse` where `ChatResponse = {text, tokens_in, tokens_out, cost_usd, model_used}`. LLM client logs spend internally (sets `is_fallback=1` when called as fallback). On HTTP 429 it raises `RateLimited(model)`; the LLMWorker catches and retries with `fallback`, then appends a `model_swapped` event via the orchestrator's event callback.
6. **Orchestrator → tool dispatcher**: `tools.dispatch(name: str, args: dict, ctx: ToolContext) -> ToolResult` where `ToolContext = {session_id, sandbox_path, stage}` and `ToolResult = {ok: bool, result: dict | None, error: dict | None}`. Errors are returned as data, never raised.
7. **Tool dispatcher → guardrails**: pre-dispatch checks via pure functions `guardrails.is_tool_allowed(name, allow_list)`, `guardrails.is_path_safe(path, sandbox_path)`. A failed check returns `ToolResult(ok=False, error=…)` AND raises a `tool_failed` alarm.
8. **Orchestrator → post-hook chain**: after a successful `write_file` to the sandbox, the orchestrator calls `post_hooks.run(sandbox_path) -> PostHookReport` (validate → regen SEO → git commit). Invisible to the worker — no tool, no envelope field.
9. **Orchestrator → checkpoints**: `checkpoints.evaluate(name, material) -> CheckpointResult`. Called after relevant events (post-hook finished → `site_valid` + `seo_artifacts_present`; user approval received → `*_approved` / `*_confirmed`).
10. **Anything → alarms**: `alarms.raise(type, severity, context, recommended_action, stage)` writes a row + appends `alarm_raised` event. Callers: guardrails (cap hit, denied tool), tool dispatcher (tool error), worker (envelope parse fail after repair), checkpoints (fail).
11. **HITL pause/resume**: `ask_user` / `request_approval` tools write a `pending_question` material with `pending=1`, set session status to `awaiting_human`, return a sentinel the orchestrator recognizes as "exit cleanly." `POST /sessions/{id}/answer` writes a `user_answer` / `user_approval` material, flips status to `active`, calls `run_until_pause` again. The next worker turn sees the answer as a `tool` message in `messages`.
12. **Stage→worker selection**: `domain.worker_for_stage(stage) -> Worker` is the single point where the chat/code split happens. Stages: `bootstrap`, `mockup` → chat; `build` → code. New stages added by editing one map.

## 15. Module responsibilities

- `orchestrator.py` — the loop & state machine; no SQL, no HTTP, no LLM calls.
- `store.py` — all SQL; pure persistence; returns dicts/dataclasses, not ORM objects.
- `worker.py` — `Worker` Protocol + `LLMWorker` + `MockWorker`.
- `llm.py` — OpenRouter HTTP + spend logging.
- `envelope.py` — Pydantic models only.
- `guardrails.py` — pure functions, no I/O.
- `checkpoints.py` — registry + pure evaluator functions.
- `alarms.py` — `raise()` writes via `store`.
- `validators.py` — pure HTML (`html5lib`) / CSS (`tinycss2`) / SEO (stdlib `xml.etree`) functions; reused by post-hooks and `site_valid` checkpoint. JS validation deferred (no good pure-Python option).
- `tools/*.py` — one file per tool; each is `def name(args, ctx) -> ToolResult`.
- `post_hooks.py` — the validate→SEO→git chain that runs after every `write_file`.
- `domain/website_builder.py` — system prompt, tool allow-list, checkpoint set, stage→worker map, restaurant seed brief.
- `app.py` — FastAPI routes; thin; delegates to orchestrator + store.

## 16. Demo flow (Maria's restaurant)

End-to-end path the demo video walks through:

1. **Bootstrap.** Session created; orchestrator routes to chat worker. Bootstrap skill is injected via `WorkerContext.system_prompt`. Worker batches `ask_user` calls (~3 rounds: name + industry, contact + location, aesthetic + palette) — each round pauses the session, the user answers via the chat input area in `_session_main.html`, loop resumes. Brief is assembled and persisted as a `business_brief` material.
2. **Brief approval.** Worker calls `request_approval` with the full brief summary. User approves → `user_approval` material → `business_brief_confirmed` checkpoint passes → stage transitions to `mockup`.
3. **Mockup.** Chat worker emits a `layout_spec` material, calls `render_mockup` → ASCII mockup with regions → `mockup_renders` checkpoint passes → worker calls `request_approval` → user approves → `mockup_approved` checkpoint passes → stage transitions to `build`.
4. **Site generation (iteration 1 — engineered failure).** Code worker calls `write_file` for `index.html`. **System prompt nudges the worker to omit `<title>`.** Post-hook chain runs: validate flags `has_title=false`; `site_valid` checkpoint **fails**; `tool_failed` alarm raised on validator failure; worker sees alarm in next `WorkerContext.state.last_alarm`.
5. **Site generation (iteration 2 — fix).** Code worker reads alarm context, calls `write_file` again with `<title>` added. Post-hook chain: validate passes; SEO files regenerated; git commit succeeds; `site_valid` and `seo_artifacts_present` checkpoints **pass**.
6. **Final.** Worker emits a `final` envelope summarizing the build. Session → `completed`. UI shows iframe of the generated site, full event timeline, both checkpoint pass/fail rows, the alarm, and total spend (under $1).

If a `MODEL_CHAT` or `MODEL_CODE` 429 occurs during the live demo, the swap is visible as a `model_swapped` event row and an `is_fallback=1` badge — the **bonus pillar (second worker swapped in)** is exercised live.

## 17. Rubric mapping

For each PRD pillar, the v1 implementation file(s) and the demo moment(s) where it's visible.

### Guardrails
- **Files:** `harness/services/guardrails.py` (pure functions), `harness/services/post_hooks.py` (auto chain), `harness/services/tools/files.py` (sandbox enforcement), allow-list in `harness/domain/website_builder.py`.
- **Visible in demo:** the post-hook chain firing after every `write_file` (timeline shows `post_hook_run` events), the `spend_cap_reached` / `iteration_limit_reached` alarm types pre-defined in the spec, and the sandbox refusal if the worker tried a path outside `data/sites/{uuid}/`. Declared, not implicit.

### Checkpoints
- **Files:** `harness/services/checkpoints.py` (registry of 5 named evaluators), `harness/services/validators.py` (engines used by `site_valid` and `seo_artifacts_present`).
- **Visible in demo:** the 5 named checkpoint rows appearing in the session detail page with `status` + `criteria_results`. The engineered `site_valid` failure-then-pass on iterations 1 and 2 is the rubric's "behavior changes meaningfully from feedback" Must.

### Material handling
- **Files:** `harness/models/envelope.py` (typed `WorkerContext` in / `WorkerResponse` out), `harness/services/store.py` (`material` table persistence by `direction`+`stage`+`type`), `harness/services/tools/*` (typed ToolResult).
- **Visible in demo:** every `ask_user` / `request_approval` / `write_file` writes a material row with the closed-set `type`. The brief, layout spec, mockup, site files, validation results, and user answers all appear as material rows tied to events.

### Alarms
- **Files:** `harness/services/alarms.py` (`raise_alarm`), `harness/models/enums.py` (closed `AlarmType` set), alarm rows in per-session DB.
- **Visible in demo:** the engineered `tool_failed` (validator-failure) alarm on iteration 1 — UI shows a row with `{type, severity, context, recommended_action}`. The presence of `spend_cap_reached`, `iteration_limit_reached`, `output_schema_violation` as defined types (even if not triggered in the happy path) is shown in `HARNESS.md` and the code.

## Open contradictions

None found between the canonical plan and the supporting docs at write time. Two items worth flagging for implementation vigilance:

- **`alarms.triggered_by_event_id` is intentionally not FK-enforced** (the plan documents this — `events.alarm_id` going the other way would cycle). Tests must not assume IntegrityError on a bad value here; assert on logical correctness instead.
- **Post-hook failures still complete the chain** (validate → SEO → git all run regardless), but each failure raises its own `tool_failed` alarm with a distinct `error_kind`. Be careful not to short-circuit on the first error — the `PostHookReport` aggregates all three step outcomes.
