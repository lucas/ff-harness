# HTTP API Reference — Harness v1

The harness exposes five HTTP routes, all served by the FastAPI app in `harness/api/app.py`. Routes are thin: they validate input, delegate to `harness/services/store.py` or `harness/services/orchestrator.py`, and serialize the result. No business logic lives in handlers.

## Conventions

These conventions apply to every route. Endpoints document only deviations.

- **Identifiers.** All `id` fields (`session_id`, `material_id`, `event_id`, `checkpoint_id`, `alarm_id`) are UUID7 strings stored as `TEXT` (full canonical form, e.g. `0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d`). UUID7 is time-ordered so ordering by `id` yields chronological order. IDs are minted server-side by `harness/models/ids.new_id()`; clients never supply them.
- **Timestamps.** Every `*_at` / `ts` field is an ISO 8601 string in UTC with second precision, e.g. `2026-06-13T14:22:31Z`.
- **Money.** Every `*_usd` field is a float in US dollars. Free-tier OpenRouter calls record `0.0`.
- **Content type.** All requests and responses are `application/json; charset=utf-8`. Request bodies must be valid JSON; missing or malformed bodies yield `400`.
- **Errors.** Non-2xx responses share the shape `{"error": "<short_code>", "detail": {...}}`. `error` is a stable machine code (`not_found`, `invalid_body`, `conflict`, `internal`); `detail` is a free-form dict with diagnostic fields.
- **Status codes used.** `200 OK`, `201 Created`, `400 Bad Request`, `404 Not Found`, `409 Conflict`, `500 Internal Server Error`. No 3xx, no auth (single-user local app).
- **Synchrony.** All routes are synchronous. `POST /sessions/{id}/resume` and `POST /sessions/{id}/answer` block until the orchestrator hits a pause/terminal/cap and may take many seconds.

---

## `POST /sessions`

Create a new session and persist its row in the core DB.

- **Method:** `POST`
- **Path:** `/sessions`
- **Query params:** none.
- **Request body:** `{}` (empty object). v1 has no per-session config — the domain bundle decides everything.
- **Response body (201):**
  ```json
  {
    "session_id": "0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
    "status": "active",
    "current_stage": "bootstrap"
  }
  ```
- **Status codes:**
  - `201` — session created. Per-session DB at `data/sessions/{session_id}.db` initialized with all four tables.
  - `400` — body is not valid JSON.
  - `500` — DB initialization failed.
- **Called by:** `index.html` "New session" form (POSTs, then redirects to `/sessions/{id}`).
- **No materials pre-populated.** The session is created empty. The bootstrap flow (chat worker collects info via `ask_user`; user approves via `request_approval(subject='business_brief', details=…)`; orchestrator persists `details` as a `business_brief` material on approval) drives the brief. `RESTAURANT_SEED_BRIEF` in `harness/domain/website_builder.py` is retained for opt-in demos / tests but is NOT auto-seeded here.

---

## `GET /sessions`

List every session in the core DB, newest first (ordered by `id` descending — UUID7 is time-ordered).

- **Method:** `GET`
- **Path:** `/sessions`
- **Query params:** none in v1.
- **Request body:** none.
- **Response body (200):**
  ```json
  {
    "sessions": [
      {
        "id": "0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
        "status": "awaiting_human",
        "current_stage": "build",
        "created_at": "2026-06-13T14:00:00Z",
        "updated_at": "2026-06-13T14:08:11Z"
      },
      {
        "id": "0190a8a1-1234-7abc-9def-1234567890ab",
        "status": "completed",
        "current_stage": "done",
        "created_at": "2026-06-12T18:42:09Z",
        "updated_at": "2026-06-12T18:55:01Z"
      }
    ]
  }
  ```
- **Status codes:**
  - `200` — always, even when the list is empty (returns `{"sessions": []}`).
  - `500` — DB read failed.
- **Called by:** `index.html` (rendered server-side on page load; the user clicks the manual "Refresh" control to re-fetch — there is no automatic polling).

---

## `GET /sessions/{id}`

Full session detail: the session row plus every log surface needed to render the session detail template without further fetches.

- **Method:** `GET`
- **Path:** `/sessions/{id}`
- **Path params:** `id` — session UUID7.
- **Query params:** none in v1.
- **Request body:** none.
- **Response body (200):**
  ```json
  {
    "session": {
      "id": "0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
      "status": "awaiting_human",
      "current_stage": "build",
      "iter_since_approval": 3,
      "created_at": "2026-06-13T14:00:00Z",
      "updated_at": "2026-06-13T14:08:11Z"
    },
    "events": [
      {
        "id": "0190a8d4-9b1d-7000-8000-000000000001",
        "ts": "2026-06-13T14:00:01Z",
        "type": "worker_input",
        "stage": "bootstrap",
        "payload": {"model": "deepseek/deepseek-v4-flash:free", "messages_count": 2, "tokens_estimate": 412},
        "material_id": null,
        "checkpoint_id": null,
        "alarm_id": null
      }
    ],
    "checkpoints": [
      {
        "id": "0190a8d4-9c10-7000-8000-000000000002",
        "name": "site_valid",
        "stage": "build",
        "status": "fail",
        "criteria_results": {"html5_parses": true, "has_title": false, "has_meta_viewport": true, "has_lang": true, "has_h1": true, "css_parses": true},
        "material_id": "0190a8d4-9c00-7000-8000-000000000003",
        "created_at": "2026-06-13T14:07:55Z"
      }
    ],
    "alarms": [
      {
        "id": "0190a8d4-9c12-7000-8000-000000000004",
        "type": "tool_failed",
        "severity": "error",
        "context": {"tool": "write_file", "args": {"path": "../etc/passwd"}, "error_kind": "path_outside_sandbox", "error_message": "path escapes sandbox"},
        "recommended_action": "Reject the write and ask the worker to retry with a path inside the sandbox.",
        "stage": "build",
        "triggered_by_event_id": "0190a8d4-9c11-7000-8000-000000000005",
        "resolved": false,
        "created_at": "2026-06-13T14:07:30Z"
      }
    ],
    "pending_materials": [
      {
        "id": "0190a8d4-9c20-7000-8000-000000000006",
        "direction": "out",
        "stage": "build",
        "type": "pending_question",
        "content": {"question": "The site failed validation 3 times. Continue?", "options": ["continue", "abort"]},
        "pending": true,
        "created_at": "2026-06-13T14:08:11Z"
      }
    ],
    "spend_summary": {
      "total_usd": 0.0412,
      "by_model": {
        "deepseek/deepseek-v4-flash:free": 0.0,
        "deepseek/deepseek-v4-flash": 0.0412,
        "qwen/qwen3-coder:free": 0.0
      },
      "fallback_count": 2
    },
    "llm_calls": [
      {
        "id": "0190a8d4-9b1e-7000-8000-000000000010",
        "ts": "2026-06-13T14:00:02Z",
        "model": "deepseek/deepseek-v4-flash:free",
        "is_fallback": false,
        "request_messages": [
          {"role": "system", "content": "you are a..."},
          {"role": "user", "content": "Make me a homepage."}
        ],
        "request_options": {"response_format": {"type": "json_object"}, "temperature": 0.2},
        "response_text": "{\"type\":\"tool_call\",\"tool\":\"ask_user\",\"args\":{\"question\":\"What is your business name?\"}}",
        "finish_reason": null,
        "tokens_in": 412,
        "tokens_out": 38,
        "cost_usd": 0.0,
        "status": "ok",
        "error_message": null,
        "related_event_id": "0190a8d4-9b1d-7000-8000-000000000001",
        "related_material_id": null,
        "created_at": "2026-06-13T14:00:02Z"
      }
    ]
  }
  ```
- **Notes on shape:** `events` is ordered ascending by `id`. `checkpoints` and `alarms` are ordered ascending by `created_at`. `pending_materials` includes only rows with `pending=1` and is the input list for the awaiting-human form. `spend_summary` is computed by `store.spend_summary_for_session(session_id)` — `total_usd` sums all `cost_usd`, `by_model` groups by exact model string, `fallback_count` counts rows where `is_fallback=1`. `llm_calls` is the most recent 50 LLM API attempts in chronological (id ASC) order; `request_messages` and `request_options` are parsed JSON objects, not nested strings. `status` is one of `ok | rate_limited | transport_error | repair_retry | parse_error`. Both `spend_summary` (rolled up from core-DB `spend_log`) and `llm_calls` (per-session full payloads) are written on every successful API attempt; the two are intentionally separate (lean cross-session ledger vs. full audit log).
- **Status codes:**
  - `200` — session found.
  - `404` — no session with that id; `error="not_found"`.
  - `500` — DB read failed.
- **Called by:** `session.html` on initial render (polling intentionally removed in v1 — the user clicks the Refresh control to re-fetch).

---

## `POST /sessions/{id}/resume`

Drive the orchestrator until it hits a pause, terminal state, or cap. Synchronous — the request blocks for the duration of the loop.

- **Method:** `POST`
- **Path:** `/sessions/{id}/resume`
- **Path params:** `id` — session UUID7.
- **Query params:** none.
- **Request body:** `{}` (empty object).
- **Behavior:** calls `orchestrator.run_until_pause(session_id)`. The orchestrator loads session state, runs turns via the stage-mapped worker, dispatches tools, runs the post-hook chain after every `write_file`, evaluates checkpoints, persists events/materials/alarms, and exits cleanly when it hits any of: a `final` envelope, an `escalate` envelope, an `ask_user`/`request_approval` tool call, the 10-iteration human-approval gate, or the $1/day spend cap.
- **Auto-unstick on `awaiting_human`:** if the session is already `awaiting_human` when the request arrives, the handler calls `orchestrator.force_continue(...)` BEFORE `run_until_pause`. That helper auto-approves any pending `continuation_approval` material (writes a `user_approval` row with `auto_approved_via_resume: true`, appends a `human_resumed` event, marks the pending resolved), resets `iter_since_approval` to 0, and flips status back to `active`. Real content gates — freeform `ask_user` pendings and subject-based `approval` pendings (e.g. `business_brief`, `mockup`) — are deliberately left pending; those still require an explicit `POST /sessions/{id}/answer`. The user's mental model is "/resume means continue past whatever safety cap is blocking me."
- **Response body (200):**
  ```json
  {
    "session_id": "0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
    "status": "awaiting_human",
    "current_stage": "build",
    "terminal": false,
    "paused_reason": "awaiting_human"
  }
  ```
  - `terminal` is `true` only when `status` is `completed` or `failed`; otherwise `false`.
  - `paused_reason` is one of `"awaiting_human"`, `"spend_cap"`, `"turn_cap"`, or `null` (the latter when `terminal=true`).
- **Status codes:**
  - `200` — orchestrator returned cleanly (including pause states).
  - `404` — no session with that id; `error="not_found"`.
  - `409` — session is already `completed` or `failed`; `error="conflict"`, `detail={"status": "..."}`.
  - `500` — unhandled orchestrator exception (the orchestrator catches its own errors into the event log; a 500 here means a bug).
- **Called by:** `session.html` "Resume" button.

---

## `POST /sessions/{id}/answer`

Submit a user's response to a pending question or approval request, then resume the loop. The body shape depends on the `content.kind` of the pending `pending_question` material:

- `kind == "approval"` — originated from a `request_approval` tool call. Expects `approved` (bool) and optional `notes`. Persisted as a `user_approval` material with `content.kind = "approval"`.
- `kind == "continuation_approval"` — written by the orchestrator when the iter cap or spend cap trips. Expects `approved` (bool) and optional `notes`. Persisted as a `user_approval` material with `content.kind = "continuation_approval"` so the orchestrator can distinguish a "continue past the cap" decision from a brief/mockup approval. A denial (`approved=false`) keeps the session paused; an approval flips it back to `active` and the loop resumes.
- missing `kind` (or any other value) — originated from `ask_user`. Expects `answer_text`. Persisted as a `user_answer` material.

The handler reads the pending material's `content.kind` to decide which `MaterialType` to persist (`user_answer` vs `user_approval`) and to set the answer's `kind` field on the persisted row.

- **Method:** `POST`
- **Path:** `/sessions/{id}/answer`
- **Path params:** `id` — session UUID7.
- **Query params:** none.
- **Request body:**
  ```json
  {
    "material_id": "0190a8d4-9c20-7000-8000-000000000006",
    "answer_text": "Continue.",
    "approved": null,
    "notes": null
  }
  ```
  - `material_id` (required) — the `pending_question` material the user is responding to.
  - `answer_text` (optional) — required when the pending material backs `ask_user`.
  - `approved` (optional bool) — required when the pending material backs `request_approval`.
  - `notes` (optional string) — only meaningful with `approved`.
- **Behavior:**
  1. Load the pending material; `404` if missing, `409` if not pending.
  2. Persist either a `user_answer` material (`content={"answer_text": ...}`) or a `user_approval` material (`content={"approved": ..., "notes": ...}`).
  3. `mark_material_resolved(material_id)` on the original pending row.
  4. Update session `status` to `active`.
  5. Append a `human_resumed` event.
  6. Call `orchestrator.run_until_pause(session_id)`.
- **Response body (200):** identical shape to `POST /sessions/{id}/resume`.
- **Status codes:**
  - `200` — answer accepted and orchestrator returned cleanly.
  - `400` — body invalid, or shape doesn't match the pending material's type (e.g. `approved` missing for an approval request); `error="invalid_body"`.
  - `404` — session or material not found; `error="not_found"`.
  - `409` — material is not pending (already answered or never was); `error="conflict"`.
  - `500` — unhandled exception during resume.
- **Called by:** the chat input area in `_session_main.html` (the pending-material form lives in the same partial as the chat log; `awaiting.html` was removed when the UI moved to the chat-first layout). Three button affordances depending on `content.kind`: Approve/Deny (for `approval`), Approve/Stop (for `continuation_approval`), or Send (for freeform `ask_user`).

---

## `POST /sessions/{id}/rewind`

Rewind a session to a previous `awaiting_human` event. Destructive but tracked: every event / material / checkpoint / alarm whose UUID7 id is greater than `target_event_id` is deleted, the original pending material is re-pended, a `rewound` audit event is appended, and the session row is reset to `awaiting_human` at the target's stage with `iter_since_approval=0`. The orchestrator loop is NOT invoked — the user submits a different answer via `POST /sessions/{id}/answer` to drive the loop forward. `spend_log` (core DB) is intentionally untouched: it represents real cost.

- **Method:** `POST`
- **Path:** `/sessions/{id}/rewind`
- **Path params:** `id` — session UUID7.
- **Query params:** none.
- **Request body:**
  ```json
  {"target_event_id": "0190a8d4-9c20-7000-8000-000000000006"}
  ```
  - `target_event_id` (required, non-empty) — the `awaiting_human` event the session is reverting to.
- **Behavior:**
  1. Validate the session exists; otherwise `404`.
  2. Validate the target event exists and is type `awaiting_human`; otherwise `400`.
  3. In one transaction inside the per-session DB: delete events / alarms / checkpoints / material whose `id > target_event_id`, `UPDATE material SET pending = 1` on the target's `payload.material_id`, append a `rewound` event with the counts.
  4. Update the core session row: `status='awaiting_human'`, `current_stage` from the target event's `stage`, `iter_since_approval=0`.
- **Response body (200):**
  ```json
  {
    "session_id": "...",
    "target_event_id": "...",
    "target_event_payload": {"material_id": "...", "reason": "ask_user"},
    "removed_events": 12,
    "removed_materials": 4,
    "removed_checkpoints": 2,
    "removed_alarms": 0,
    "repended_material_id": "...",
    "rewind_event_id": "..."
  }
  ```
- **Status codes:**
  - `200` — rewind applied; report dict returned.
  - `400` — body missing/invalid, or `target_event_id` does not exist / is not an `awaiting_human` event; `error="bad_request"`.
  - `404` — session not found; `error="not_found"`.
- **Called by:** the Rewind button rendered on each `awaiting_human` row in the events table inside the Details accordion (`harness/templates/_session_main.html`). Confirms via a `confirm()` dialog before POSTing, then reloads the page on success.

---

## Out of scope for v1

`PATCH /sessions/{id}`, `DELETE /sessions/{id}`, paginated event fetch, auth — all deferred. The six routes above are sufficient for the demo flow and the four rubric pillars.
