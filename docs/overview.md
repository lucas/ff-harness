# Harness — Overview

## Purpose
A harness is the framework an AI agent runs inside: the agent focuses on the task, the harness owns the constraints, evaluation, I/O, and failure handling. This one turns a vague request from a non-technical person into a working web page — safely, with the human confirming intent *before* any building begins.

## User story
A **website builder for non-technical users.** A user submits a basic idea or change ("a landing page for my bakery"). The harness **clarifies intent first** — asking questions to gather details, then confirming the layout with a **basic ASCII mockup** before it builds. On approval it generates a simple, **SEO-optimized HTML + CSS + JS** page — regenerating `sitemap.xml` after each iteration — and **auto-commits every change to a local git repo**, so all versions are tracked and revertable.

## The harness
A **durable, resumable run orchestrator**. Each run is a **session** with its own SQLite DB; a core DB indexes users/sessions; an append-only **event log** is the source of truth, so a run survives crashes and can replay from any checkpoint. The worker is a thin, swappable plug. Priorities: **durability, resilience, simplicity.**

## The four pillars

**1. The Loop.** The bounded control cycle that drives the agent each turn: call the worker → validate its JSON output → dispatch (run a tool / finish / **escalate to ask the user**) → verify & persist → advance or recover. Resumable from any checkpoint; capped so a confused agent can't run away.

**2. Tools.** Typed functions the agent can call; the harness validates arguments, executes, and returns results as data. For the website builder, the 7 worker-callable tools are:
- `ask_user` — ask the user clarifying questions
- `request_approval` — show the ASCII mockup, await sign-off
- `save_business_brief` — persist the user-approved brief into session memory
- `render_mockup` — ASCII wireframe from the layout spec
- `read_file` / `write_file` / `list_files` — sandboxed page-file I/O

> SEO regeneration, site validation, and git commits run automatically after each `write_file` (the harness post-hook chain) and are not callable by the worker — see `docs/v1-spec.md`.

**3. Guardrails.** Declared constraints, enforced: tool allow-lists, turn/token/time caps, a **$1/day spend ceiling**, sandboxed file & git paths, and a gate that pauses for **human approval every 10 iterations**.

**4. Observability.** Every step is recorded to an append-only **event log in the session's SQLite database** — a structured entry per **LLM turn** (model, tokens, **cost**, latency), every tool call, and every guardrail decision — powering replay and cost tracking. Failures surface as **alarms**: named signals with severity, context, and a recommended action (`spend_cap_reached`, `iteration_limit_reached`, `output_schema_violation`).

## Beyond a single run
Workers can spawn **sub-agents** (isolated sessions, fresh context); **compaction** lets a session continue past the model's context limit without losing the durable record. Deployed as a **FastAPI app in Docker** with a bind-mounted SQLite volume and a simple HTML/JS/CSS page that serves as the human-in-the-loop and observability surface.
