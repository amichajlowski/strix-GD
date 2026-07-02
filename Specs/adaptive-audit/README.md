# Adaptive Audit — Findings-Driven Steering

## Goal

Make Strix behave like an experienced human red teamer who, at natural
breakpoints, **stops, reviews what has been discovered, revises the working
theory of the target, and re-steers the audit** — instead of running each agent
to completion against a fixed plan and only reconciling at the very end.

Today the only findings→steering mechanism is the **QA gap engine**
(`strix/tools/qa_loop/rules.py`), and it runs once, at the *finish gate* — too
late to redirect effort mid-audit. Between start and finish, direction lives
only in the root agent's head (context that rots over a long run) and in
free-form `notes`.

This spec adds a small, delegated **reflection loop**: at each meaningful
boundary a focused **reflection step** (a code path making one structured model
call, not an agent) reads the shared discovery state, updates a structured
**working thesis**, and hands the root a revised plan to act on. It
is the missing *synthesis + direction* layer on top of the raw state stores
(`loot`, `target_profile`, `notes`) shipped by Tool Awareness.

## The human behaviour we are modelling

1. Finish a probe / a sub-task → **step back**.
2. Re-read everything learned so far (not just the last result).
3. **Revise assumptions** — "I thought there was no WAF; the 403 spike says
   otherwise." Old belief is superseded, not silently left to rot.
4. **Re-prioritise** — promote the lead the new finding just opened, drop the
   dead one.
5. **Re-steer** — retask or spawn effort where the evidence now points.

## The two features (build order)

| # | Feature | Surface | What it is |
|---|---------|---------|------------|
| 1 | **Audit state store** | `get_audit_state`, `update_audit_state` | The evolving working thesis: hypotheses, **assumptions with confidence + supersede history**, prioritised leads. Mirrors the `notes`/`loot` store template. Covers the original "#1 audit memory" **and** "#3 assumption revision" — revision is a property of this store, not separate machinery. |
| 2 | **Reflection loop** | `strix/core/reflection.py` + `on_agent_end` trigger | A dedicated **code path** (not an agent) that fires deterministically when a specialist finishes: it reads the blackboard, makes one structured LLM call on the run's own model, and writes the revised thesis/leads into the audit state. The root reads it and steers. Extends the QA gate to enforce that top leads are pursued. |

Feature 1 is independently useful (a place for the audit thesis to live) and
ships first. Feature 2 is the loop that keeps it current.

## Design commitments (read before building)

- **Blackboard, not message-passing.** Findings already propagate through
  shared, persisted, all-agent-visible stores (`notes`, `loot`,
  `target_profile`, and the new `audit_state`). The reflection *reads* the
  blackboard and *writes* the thesis back to it. No per-finding plumbing.
- **Single decision authority, enforced structurally.** Analysis is a dedicated
  code path with **no tools** — it can only write `audit_state`, so it *cannot*
  spawn/stop/interrupt/test/report. *Deciding and acting* stays with the root.
  Authority is singular by construction, not by prompt.
- **Deterministic trigger for every audit.** The reflection fires from an
  `on_agent_end` lifecycle hook (already-wired `RunHooks`) when a specialist
  finishes — one code path, no size heuristic, and it does not depend on the root
  model remembering to do anything.
- **Reflect at boundaries, never per tool.** A specialist finishing is a coherent
  unit of new information; reflecting on partial mid-batch state produces
  flip-flopping. Near-simultaneous completions are coalesced (single-flight) into
  one reflection, not N.
- **Steering is supersede-with-reason, not re-derive-from-scratch.** The
  reflection reads its own prior thesis and must justify a *change* (supersede an
  assumption with a reason + new confidence), never rebuild blind. The keystone
  that keeps steering coherent across boundaries.
- **Same model, structured + tolerant.** The reflection runs on the run's own
  model (`STRIX_LLM`) — not a downgraded one — with structured output and
  tolerant parsing (retry once, else skip cleanly), so weak local-model JSON
  never crashes the run or corrupts the store.

## Non-goals (do NOT build these)

- **A rules engine that auto-acts.** The reflection *proposes* (writes thesis +
  leads); the root LLM *decides*. Do not encode hard "if X then spawn Y" rules
  that bypass the model. The deterministic QA gap rules stay *advisory* (surface
  gaps; the model acts).
- **A second decision authority.** The reflection has no tools; it only writes
  the thesis. Only the root acts.
- **Per-tool reflection.** See cadence commitment above.
- **A strategist agent / new agent type.** The reflection is a code path
  (`reflection.py`) making one direct `litellm` call — not a spawned child agent
  (which would fight the child-cap, inherit the root's context, hold the full
  toolset, and self-trigger — see [05-review-findings.md](05-review-findings.md)
  §0.1). litellm is already a dependency and globally configured, so this is not
  a new inference stack, just a direct use of the existing one.
- **Heavy schemas / a knowledge graph.** Keep `audit_state` flat and bounded,
  exactly like `loot`/`target_profile`.

## Conventions every task must follow

- **Mirror `strix/tools/notes/tools.py`** for the store: module dict +
  `threading.RLock` + atomic `_persist` (tempfile + `Path.replace`) +
  `hydrate_audit_state_from_disk(state_dir)`.
- **Pure helpers hold the logic** (revision/supersede reconciliation, lead
  ordering, qa-signal extraction) so unit tests need no LLM, Docker, network, or
  Caido.
- **Tools are `@function_tool` async wrappers** offloading via
  `asyncio.to_thread(...)`, returning `json.dumps(..., ensure_ascii=False,
  default=str)`, taking `ctx: RunContextWrapper` first.
- **Secret discipline.** `audit_state` holds *derived* facts (hypotheses,
  assumptions, leads) — **never raw secret values**. Reference loot by
  `loot_id`, never by value (use `mask_value` if a hint is unavoidable). Same
  bundle/transcript caveats as `target_profiles.json` (see 01 §Secret
  discipline).
- **Bound everything persisted.** Cap list lengths, string lengths, supersede
  history depth. No unbounded growth.
- **Use `XXXX`** for every placeholder identifier/secret/domain in docs, tests,
  and examples.

## Documents

- [01-architecture.md](01-architecture.md) — blackboard model, reflection/root
  split, deterministic trigger + cadence, propagation paths, wiring anchors,
  secret discipline, consistency mechanisms, testing strategy.
- [02-audit-state.md](02-audit-state.md) — Feature 1 spec (store + assumptions +
  supersede/revision).
- [03-strategist-loop.md](03-strategist-loop.md) — Feature 2 spec: the reflection
  code path (`reflection.py`), `on_agent_end` trigger, structured/tolerant model
  call, propagation, QA-gate integration, consistency. *(Filename kept for link
  stability; the feature is the reflection loop, not a strategist agent.)*
- [04-task-list-and-tests.md](04-task-list-and-tests.md) — ordered checklist +
  consolidated test plan.
- [05-review-findings.md](05-review-findings.md) — reviewer passes; §0 lists the
  fixes applied to this spec (§0.1 = the functional review that drove the
  code-path vehicle).

## Definition of done

```bash
uv run pytest tests/test_audit_state.py tests/test_reflection_loop.py
uv run pytest        # no pre-existing test regressed
make lint
make type-check
```

All green (feature delta adds zero new lint/type errors — see the Tool
Awareness build for the pre-existing baseline), plus the per-feature acceptance
criteria in each document.
