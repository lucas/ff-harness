# Harness — Overview

## Purpose
A harness is the framework an AI agent runs inside: the agent focuses on the task, the harness owns the constraints, evaluation, I/O, and failure handling. This one turns a vague request from a non-technical person into a working web page — safely, with the human confirming intent *before* any building begins.

## User story
A **website builder for non-technical users.** A user submits a basic idea or change ("a landing page for my bakery"). The harness **clarifies intent first** — asking questions to gather details, then confirming the layout with a **basic ASCII mockup** before it builds. On approval it generates a simple, **SEO-optimized HTML + CSS + JS** page — regenerating **`sitemap.xml` after each iteration** — and **auto-commits every change to a local git repo**, so all versions are tracked and revertable.

## The harness
A **durable, resumable run orchestrator**. It drives a worker through staged work, persists every step, and can resume after a crash or replay from any checkpoint. The worker is a thin, swappable plug. Priorities: **durability, resilience, simplicity.** Each run is a **session** with its own SQLite DB; a core DB indexes users/sessions; an append-only **event log** is the source of truth.

## The four pillars

**1. Guardrails — declared constraints, enforced.** Tool allow-lists, turn/token/time caps, a $1/day **spend ceiling**, and a gate that pauses for **human approval every 10 iterations**.

**2. Checkpoints — explicit pass/fail gates.** Tiered verification: cheap deterministic checks first (valid HTML/CSS/JS, **SEO tags + `sitemap.xml`**, schema — $0), then an **independent verifier sub-agent**, then a **human** (e.g. confirming the ASCII mockup). Results persist for replay.

**3. Material handling — typed I/O.** The worker returns a strict JSON envelope — `tool_call`, `final`, or `escalate` — validated with Pydantic each turn. Generated files are versioned via **automatic local git commits**.

**4. Alarms — structured failure signals.** Named alarms with severity, context, and a recommended action (`spend_cap_reached`, `iteration_limit_reached`, `output_schema_violation`) drive retry, escalate, or halt.

## How a turn runs
Call the worker → validate JSON → dispatch (run a tool / finish / **escalate to ask the user**) → checkpoint → persist → advance or recover. Guardrails wrap the loop; alarms watch it.

## Beyond a single run
Workers can spawn **sub-agents** (isolated sessions, fresh context); **compaction** lets a session continue past the model's context limit without losing the durable record. Deployed as a **FastAPI app in Docker** with a bind-mounted SQLite volume and a simple HTML/JS/CSS page that serves as the human-in-the-loop and observability surface.
