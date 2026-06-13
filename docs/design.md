# Design — Components & Interfaces

High-level component map and request flow. For the narrative see `overview.md`; for full implementation detail see `implementation-architecture.md`. Diagrams are Mermaid (render in GitHub/VS Code).

> **v1 checkpoints are deterministic only (no verifier sub-agent). Diagrams below reflect the shipped v1 architecture as documented in `docs/v1-spec.md`.**

## Component architecture

```mermaid
flowchart TB
  user(["Non-technical user"])
  landing["Landing page — Jinja2 templates<br/>HITL + observability surface"]
  api["FastAPI app — api/app.py"]

  subgraph core["Harness core (domain-agnostic)"]
    orch["Orchestrator — services/orchestrator.py<br/>bounded, resumable loop"]
    store["Store — services/store.py<br/>material + event persistence"]
    guard["Guardrails — services/guardrails.py"]
    check["Checkpoints — services/checkpoints.py<br/>deterministic only (v1)"]
    alarm["Alarms — services/alarms.py"]
    posthook["Post-hooks — services/post_hooks.py<br/>validate → SEO regen → git commit"]
    valid["Validators — services/validators.py<br/>html5lib + tinycss2"]
  end

  subgraph bundle["Per-domain bundle (declared, swappable)"]
    domain["Domain config — domain/website_builder.py<br/>tools allow-list · stage map · system prompt · skills"]
    tools["Tools — services/tools/<br/>user · files · mockup · brief"]
  end

  subgraph workers["Workers (two-worker stage map)"]
    chatw["Chat worker — LLMWorker<br/>DeepSeek v4 Flash"]
    codew["Code worker — LLMWorker<br/>Qwen3 Coder"]
    mockw["MockWorker — scripted (tests)"]
  end

  llm["LLM client — services/llm.py"]
  openrouter[("OpenRouter API")]

  core_db[("Core DB · harness.db<br/>sessions · spend_log")]
  sess_db[("Session DB · sessions/{uuid}.db<br/>events · checkpoints · material · alarms · llm_calls")]
  gitrepo[("Local git repo · generated site")]

  user <--> landing
  landing <--> api
  api <--> orch

  orch --> guard
  orch --> store
  orch -- "WorkerContext" --> chatw
  orch -- "WorkerContext" --> codew
  chatw -- "JSON envelope" --> orch
  codew -- "JSON envelope" --> orch
  chatw --> llm
  codew --> llm
  llm --> openrouter
  domain -. "configures" .-> orch

  orch -- tool_call --> tools
  guard -. allow/deny .-> tools
  tools -- result as data --> orch
  tools --> gitrepo

  orch -- "after write_file" --> posthook
  posthook --> valid
  posthook --> gitrepo

  orch --> check
  orch --> alarm
  alarm --> landing

  store --> core_db
  store --> sess_db
  check --> sess_db
```

## Key interfaces
- **User ↔ Jinja2 templates ↔ FastAPI** — HTTP/HTML: chat-first UI with conversation bubbles, context-sensitive input area, details accordion, cost/alarm display.
- **FastAPI ↔ Orchestrator** — create or resume a run by `session_id`; `/resume` auto-unsticks `awaiting_human` via `force_continue`.
- **Orchestrator ↔ Worker** — the *only* agent contract: `Worker.act(WorkerContext) -> ToolCall | Final | Escalate`, a serializable, data-only boundary. Two stage-mapped `LLMWorker` instances (chat + code) wired by the domain bundle; `MockWorker` for tests.
- **Worker ↔ LLM ↔ OpenRouter** — provider details isolated in `services/llm.py`; returns tokens/cost/latency. 429 → auto-swap to fallback model.
- **Orchestrator ↔ Tools** — orchestrator hands a validated `tool_call`; guardrails allow-list it (7 tools); dispatch executes; result returns as data.
- **Orchestrator ↔ Post-hooks** — after every successful `write_file`: validate (html5lib/tinycss2) → regenerate SEO artifacts → git commit.
- **Orchestrator ↔ Guardrails / Checkpoints / Alarms** — pre/post interception, deterministic verification (5 checkpoints), 4 alarm types with state-based auto-resolve.
- **Orchestrator ↔ Store** — append events + checkpoints + materials to the per-session DB; sessions/spend_log in the core DB; `llm_calls` audit table in the per-session DB.
- **HITL** — `escalate` / iter-cap / spend-cap → `awaiting_human` → chat UI → approve/deny/answer → resume. Rewind to any prior `awaiting_human` event via `POST /sessions/{id}/rewind`.

## The turn loop

```mermaid
flowchart TD
  start(["Resume or start session"]) --> resolve_alarms["Resolve obsolete state alarms<br/>(iter/spend caps auto-clear)"]
  resolve_alarms --> assemble["Assemble WorkerContext<br/>(events + materials + system prompt)"]
  assemble --> pre{"Guardrails pre-check<br/>iter cap · spend cap"}
  pre -- "10-iter gate /<br/>$1 spend cap" --> cap_hitl["Persist continuation_approval<br/>→ awaiting_human"]
  pre -- ok --> select["Select worker by stage<br/>(chat or code)"]
  select --> act["Worker.act → LLM call"]
  act --> cjk{"CJK drift<br/>detected?"}
  cjk -- yes --> lang_retry["Retry with English-strict<br/>directive (once)"]
  lang_retry --> cjk2{"Still CJK?"}
  cjk2 -- yes --> alarm_osv["output_schema_violation alarm<br/>→ Escalate"]
  cjk2 -- no --> validate
  cjk -- no --> validate{"Valid JSON<br/>envelope?"}
  validate -- no --> repair["Repair retry (once)"]
  repair --> validate2{"Valid now?"}
  validate2 -- no --> alarm_osv
  validate2 -- yes --> dispatch
  validate -- yes --> dispatch{"Envelope type"}
  dispatch -- tool_call --> tool["Guardrails allow-list →<br/>Tools dispatch → result"]
  tool --> posthook{"write_file?"}
  posthook -- yes --> hooks["Post-hooks: validate →<br/>SEO regen → git commit"]
  hooks --> persist
  posthook -- no --> persist
  dispatch -- escalate --> hitl[["awaiting_human"]]
  dispatch -- final --> check["Checkpoints: deterministic<br/>evaluate (5 gates)"]
  check --> persist[("Persist event + material +<br/>checkpoint → session DB<br/>cost → spend_log + llm_calls")]
  persist --> done{"Done?"}
  done -- no --> assemble
  done -- yes --> finish(["Return result"])
  hitl -- "approve / answer" --> assemble
  hitl -- deny --> finish
  cap_hitl -- "/resume" --> assemble
  cap_hitl -- stop --> finish
```

**In one line:** guardrails wrap the loop, the orchestrator selects the stage-appropriate worker, validates the JSON envelope (with CJK-drift and repair retries), tools execute under allow-list with post-hooks on writes, deterministic checkpoints verify, alarms watch, and every step is persisted — resumable from any event.
