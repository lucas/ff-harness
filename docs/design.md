# Design — Components & Interfaces

High-level component map and request flow. For the narrative see `overview.md`; for full implementation detail see `implementation-architecture.md`. Diagrams are Mermaid (render in GitHub/VS Code).

> **v1 MVP omits the verifier sub-agent and `subagent.py`; checkpoints are deterministic only. The diagrams below show the full architecture — the v1-shipped subset is documented in `docs/v1-spec.md`.**

## Component architecture

```mermaid
flowchart TB
  user(["Non-technical user"])
  landing["Landing page — static/<br/>HITL + observability surface"]
  api["FastAPI app — app.py"]

  subgraph core["Harness core (domain-agnostic)"]
    orch["Orchestrator — orchestrator.py<br/>bounded, resumable loop"]
    mat["Material handling — material.py<br/>context assembly + JSON envelope"]
    ctx["Context + compaction — context.py"]
    guard["Guardrails — guardrails.py"]
    check["Checkpoints + Verification<br/>checkpoints.py / verification.py"]
    alarm["Alarms — alarms.py"]
  end

  subgraph bundle["Per-domain bundle (declared, swappable)"]
    tools["Tools registry + dispatch — tools.py"]
    skills["Skills — skills.py"]
  end

  worker["Worker, swappable — worker.py"]
  llm["LLM client — llm.py"]
  openrouter[("OpenRouter · Nemotron free")]
  sub["Sub-agent — subagent.py<br/>nested session"]

  core_db[("Core DB · harness.db<br/>users · sessions · spend_log")]
  sess_db[("Session DB · sessions/{uuid}.db<br/>events · checkpoints · material · alarms")]
  gitrepo[("Local git repo · generated site")]

  user <--> landing
  landing <--> api
  api <--> orch

  orch --> guard
  orch --> mat
  mat --> ctx
  mat -- WorkerContext --> worker
  worker -- "WorkerResponse (JSON envelope)" --> mat
  worker --> llm
  llm --> openrouter
  mat -. injects skills .-> skills

  orch -- tool_call --> tools
  guard -. allow/deny .-> tools
  tools -- result as data --> orch
  tools --> gitrepo

  orch --> check
  check -. semantic gate .-> sub
  orch --> alarm
  alarm --> landing

  orch --> core_db
  orch --> sess_db
  check --> sess_db
```

## Key interfaces
- **User ↔ Landing page ↔ FastAPI** — HTTP/JSON: submit an idea, view status/cost, answer escalations (approve/deny).
- **FastAPI ↔ Orchestrator** — create or resume a run by `session_id`.
- **Orchestrator ↔ Worker** — the *only* agent contract: `Worker.act(WorkerContext) -> WorkerResponse`, a serializable, data-only boundary (see *Swappable worker boundary* in `implementation-architecture.md`).
- **Worker ↔ LLM ↔ OpenRouter** — provider details isolated in `llm.py`; returns tokens/cost/latency.
- **Orchestrator ↔ Tools** — orchestrator hands a validated `tool_call`; guardrails allow-list it; dispatch executes; result returns as data.
- **Orchestrator ↔ Guardrails / Checkpoints / Alarms** — pre/post interception, tiered verification, structured alarms.
- **Orchestrator ↔ Store** — append events + checkpoints to the session DB; users/sessions/spend_log in the core DB; cost mirrored for the daily cap.
- **Orchestrator ↔ Sub-agent** — spawn a nested session (own DB, fresh context); only the result returns to the parent.
- **HITL** — `escalate` → `awaiting_human` → landing page → approve/deny → resume from the last checkpoint.

## The turn loop

```mermaid
flowchart TD
  start(["Resume or start session"]) --> assemble["Material: assemble WorkerContext<br/>+ compact if near context limit"]
  assemble --> pre{"Guardrails pre-check<br/>caps · spend · approval"}
  pre -- "block / 10-iter gate" --> hitl[["Escalate → awaiting_human"]]
  pre -- ok --> act["Worker.act → JSON envelope"]
  act --> validate{"Valid envelope?"}
  validate -- no --> repair["Repair retry,<br/>else output_schema_violation alarm"]
  repair --> act
  validate -- yes --> dispatch{"Envelope type"}
  dispatch -- tool_call --> tool["Guardrails allow-list →<br/>Tools dispatch → result as data"]
  tool --> persist
  dispatch -- escalate --> hitl
  dispatch -- final --> verify["Checkpoints: tiered verify<br/>deterministic → verifier sub-agent → human"]
  verify --> persist[("Persist event + checkpoint → session DB<br/>cost → spend_log · git commit")]
  persist --> done{"Done?"}
  done -- no --> assemble
  done -- yes --> finish(["Return result"])
  hitl -- approve --> assemble
  hitl -- deny --> finish
```

**In one line:** guardrails wrap the loop, material handling assembles context and validates the worker's JSON, the worker only *proposes* actions, tools execute under allow-list, checkpoints verify, alarms watch, and every step is persisted to the session DB — resumable from any checkpoint.
