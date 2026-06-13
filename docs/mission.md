# Mission — 24-Hour Harness Build Challenge

## What we're building
A **harness**: the framework an AI agent lives inside. The harness — not the agent — is what's evaluated. It provides what the agent gets "for free": constraints, evaluation of outputs, clean I/O, and failure alerts. Domain is our choice; the quality of the harness and how an agent works with it is the grade.

> "Agents focus on tasks. Harnesses focus on constraints. A well-designed harness makes constraint-handling invisible to the agent."

## Source material
- `prd.pdf` — the official brief. **This is the rubric** (the four pillars and requirements below come from it).
- `harness/1.pdf`–`harness/16.pdf` — the Fired Festival "Building an AI Harness" teaching deck (slides 1–16). **Background only**, not requirements. See `harness.md` for what we lifted from it.
- `https://fired-festival.com/harness` — event page, no usable content (title only).

## The four pillars (graded — from the PRD)
Each must be a **distinct, identifiable component, separate from the worker**:
1. **Guardrails** — constraints on behavior. Must be **declared, not implicit**.
2. **Checkpoints** — evaluate outputs with **explicit pass/fail criteria**.
3. **Material handling** — clean interfaces for passing input/output in and out.
4. **Alarms** — **structured output**: named alarm types, each with context, severity, and a recommended action.

## Requirements
**Must**
- All four pillars implemented and demonstrably separate from the worker.
- The agent's behavior **changes meaningfully** based on guardrail or checkpoint feedback.
- Guardrails declared, not implicit. Checkpoints have explicit pass/fail criteria.
- Alarms produce structured output (named types + context + severity + recommended action).
- Runs on a **real input from the engineer's own work** at demo time.
- A `HARNESS.md` covering architecture and design.

**Should**
- Swappable agent interface — dropping in a different agent needs **no harness changes**.
- Checkpoint results **persisted** — replay a run from any checkpoint forward without re-running prior stages.
- Human-in-the-loop escalation — the harness knows when to stop and ask rather than guess.

**Bonus**
- A second worker swapped in during the demo to prove portability.

## Deliverables & deadlines
**Friday June 12, 2026, 11:30 PM** (today)
- 1-page Harness Planning Document.

**Saturday June 13, 2026, 4:30 PM**
- Project repo URL.
- Deployed Harness URL.
- `HARNESS.md` (capabilities + architecture) in the repo.
- 5-minute demo video.

## Current phase
**Planning only** — not implementing yet. Immediate deliverable is the 1-page planning document. Design state and open decisions live in `harness.md`.

## Dev environment
- Working dir: `/Users/elroy/work/claude`.
- LSP configured and verified for Python, TypeScript, PHP, Go (pyright, typescript-language-server, intelephense, gopls). `~/go/bin` added to `~/.zshrc` PATH.
- Tooling preferences: PNPM (never NPM), UV; 7-day minimum release age for both. Parameterized SQL, watch for N+1.
