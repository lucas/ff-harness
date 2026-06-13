# Harness — Design Context

Design notes for the harness we're planning. See `mission.md` for the challenge, requirements, and deadlines. **No final architecture is committed yet** — domain/stack are pending user confirmation (see Open Decisions).

## Pillar vocabulary: PRD vs. the deck
The PRD (rubric) and the Fired Festival deck use **different** four-pillar models. We build to the **PRD's four pillars**; the deck is supporting material.

| PRD pillar (graded) | Deck equivalent | Notes |
|---|---|---|
| Guardrails | Guardrails (input/action/output layers, allow-list, hard limits) | Maps cleanly. |
| Material handling | Tools + the message loop (typed contracts, results returned as data) | Maps cleanly. |
| Alarms | Observability + "fail as data" | Must **upgrade** to named types + severity + recommended action — deck only does raw traces / error strings. |
| Checkpoints | Eval gates, turn caps, human-approval | **Largely net-new.** Deck has no explicit pass/fail-gate pillar. |

**Where we score beyond the deck:** structured **alarms** and explicit **checkpoints** are the differentiators the deck doesn't spell out.

## Recommended direction (pending confirmation)
**Code-change agent harness.** An agent edits code in a repo to satisfy a real ticket; the harness governs it via the four pillars.

Why this fits best:
- Demonstrates the hardest Must — "behavior changes meaningfully from feedback" — via a write → checkpoint (tests / LSP / lint) → fix loop.
- Real input = a real ticket + repo from the engineer's own work.
- Swapping the worker model (Claude → another) covers the swappable-agent Should and the portability Bonus.
- Aligns with the engineer's strengths: multi-language, code-quality, SQL-safety, docs discipline.

### How the pillars would map for this domain
- **Guardrails** — declared: tool allow-list, sandbox/scope limits, no-secrets, turn/token/time/spend caps, parameterized-SQL and no-N+1 rules.
- **Checkpoints** — pass/fail gates: tests pass, LSP diagnostics clean, lint clean, docs updated. Persisted so a run can replay from any gate forward.
- **Material handling** — typed tool contracts (read/write files, run tests, grep); ticket/diff in, structured results back as data.
- **Alarms** — named, severity-tagged, with recommended action (e.g. security issue detected, scope creep, repeated checkpoint failure, turn-limit hit).

## Reusable patterns lifted from the deck
- **Build order** (deck slide 16): loop first → add tools (one typed contract at a time) → wrap guardrails → instrument from day one.
- **Examples catalog** (deck slide 15) — "swap the toolset and you have a new agent": coding agent (read/write/test/grep · sandbox+diff review), research assistant, support triage, data copilot (SQL · read-only role+row limits), inbox agent (draft-only), ops runbook (dry-run+on-call confirm).
- **Patterns**: bounded turn cap, single dispatch choke point, tool allow-list, fail-as-data, typed tool contracts, human-approval gate, dry-run, least-privilege role.
- **Architecture shape** (deck slide 11): guardrails wrap the loop on input and output; observability watches every step.

## Open decisions (need user confirmation before drafting the plan)
1. **Domain / real input** — *recommend:* code-change agent on a real ticket+repo. Alternatives: review a real PR, SQL agent on a real DB, docs-maintenance on real stale docs.
2. **Stack** — *recommend:* Python (FastAPI). Alternatives: TypeScript/Node, Go.
3. **Worker** — *recommend:* Claude API behind a swappable adapter (enables the Bonus).
4. **Deploy target** — *recommend:* managed host (Render/Railway/Fly) for a stable URL. Alternatives: local+tunnel, serverless.

## Next step
Once domain + the three defaults are confirmed, write the **1-page Harness Planning Document** (due Friday June 12, 11:30 PM).
