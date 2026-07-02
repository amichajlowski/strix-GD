# Feature 2 — Strategist Reflection Loop

**Priority: build second. Depends on Feature 1 (audit state store).**

## What it does

At each boundary, a delegated **strategist** reads the blackboard, reconciles it
against the prior thesis, and writes the revised thesis / assumptions / leads
into `audit_state`. The root then reads `audit_state` and steers. A QA-gate rule
makes the finish gate block while high-priority leads are unpursued, so the loop
is enforced, not merely suggested.

**Feature 2 adds no new tool module.** It is three small pieces:

1. an `audit_strategy` **skill** (the reflection procedure),
2. a **QA-gate rule** in `qa_loop/rules.py` + its wiring in
   `_build_review_context` (the deterministic backstop),
3. **prompt** wiring so the root spawns the strategist at boundaries and all
   agents read `audit_state`.

The strategist is a normal Strix child agent spawned via the existing
`create_agent(name="Audit Strategist", skills=["audit_strategy"])`. It reaches
the blackboard with tools it already has (`get_loot`, `get_target_profile`,
`traffic_health`, `list_notes`, `get_audit_state`) and writes with
`update_audit_state`.

## The `audit_strategy` skill

Create `strix/skills/reconnaissance/audit_strategy.md` (frontmatter `name:
audit_strategy`; **filename stem must be `audit_strategy`** — `load_skills`
resolves by stem, frontmatter is stripped). It instructs the strategist to:

1. **Read everything discovered** — `get_audit_state` (the prior thesis first),
   then `get_loot`, `get_target_profile`, `traffic_health`, and
   `list_notes(category="findings")`. Do **not** re-run tools or test anything —
   this is analysis only.
2. **Reconcile against the prior thesis, do not rebuild it.** For each belief
   that new evidence changes, **supersede** the old assumption
   (`update_audit_state(assumption=..., confidence=..., supersedes=<old_id>,
   reason=...)`) rather than restating from scratch. Unchanged beliefs stay put.
3. **Revise the thesis** (`update_audit_state(thesis=...)`) — one tight
   paragraph: what the target is, what is confirmed, where the best leads are.
4. **Re-prioritise leads** — add new leads the latest findings opened
   (`update_audit_state(lead=..., priority=..., reason=..., refs=[loot_ids])`),
   and update the status of leads now pursued/dead
   (`update_audit_state(lead_id=..., lead_status=...)`).
5. **Reference by id, never by value.** Cite loot as `loot_id`, notes as
   `note_id`. Never copy a raw secret/credential/token into `audit_state`.
6. **If nothing material changed, say so and stop** — bump nothing beyond a
   short "no change" note. Do not manufacture churn.
7. `agent_finish` with a 2–3 line summary of what changed (ids of superseded
   assumptions, new high-priority leads) — the root reads this + `get_audit_state`.

Reference the skill from `strix/skills/README.md` (the `/reconnaissance` row,
alongside `environment_profiling`).

## Trigger & cadence (see 01 for the full rationale)

- **Boundary-triggered:** after a child completes (root `wait_for_message`
  returns a completion), or on a significant event (new confirmed finding,
  `traffic_health` regime change). **Never per raw tool call.**
- **Root convention → `strix/skills/coordination/root_agent.md`** (NOT
  `system_prompt.jinja` — that template has no `is_root` conditional; root-only
  guidance is auto-appended from the `root_agent.md` skill by `prompt.py`).
  Extend its "Pre-finish QA review" section with:

  ```
  - After any child agent completes (a wait_for_message returns its report),
    spawn an "Audit Strategist" with create_agent(skills=["audit_strategy"]),
    wait for it, then read get_audit_state and align your next moves to its
    current high-priority leads before dispatching new work.
  ```

  The **shared** read-`get_audit_state`-before-new-surface line goes in
  `system_prompt.jinja` (that region does exist, for all agents).

- **Deterministic backstop:** the finish gate blocks on open high-priority leads
  (below), so a skipped reflection cannot let the audit finish un-reconciled.

## Propagation

- **In:** the strategist reads the shared blackboard — it sees everything every
  child wrote, across resume. No per-finding plumbing.
- **Out (passive):** the revised `audit_state` is on the blackboard; every child
  reads `get_audit_state` before a new surface (shared prompt line); the root
  reads it after each reflection.
- **Out (active):** for an urgent steer the **root** interrupts a running child
  via the existing `send_message_to_agent` (e.g. "WAF now present — switch to
  evasion; see lead `l7g8h9`"). The strategist never does this; only the root
  acts.

## QA-gate integration (the enforcement)

- `qa_loop/tool.py::_build_review_context` — add `audit = qa_audit_summary(...)`,
  extend `signal_text` with `audit["signals"]`, carry `audit["refs"]` as
  `_audit_leads` (mirror the `qa_notes_summary` / `qa_loot_summary` wiring).
- `qa_loop/rules.py::evaluate_qa_gaps` — add one rule: every open
  `priority=high` lead with `status=open` produces a **high** (finish-blocking)
  gap: *"Pursue or explicitly defer high-priority lead: `<text>`."* Dropping or
  completing the lead (`lead_status=dropped|done`) clears the gap. This is the
  deterministic reason a skipped reflection still cannot ship an un-reconciled
  audit — **on deep scans only** (the QA gate is `scan_mode == "deep"`; see 01).
  This is the first QA rule to interpolate a **dynamic** field (`lead.text`) into
  a gap `reason`/`suggested_action`; it **must** be built so that text flows
  through the existing `_scrub_gap`/`_scrub_text` path (`qa_loop/tool.py`) before
  the review is persisted — do not hand-format an unscrubbed `reason`.
- Keep it **advisory in spirit**: the gap tells the root what is unpursued; the
  root decides to pursue or `dropped`-with-rationale. No auto-action.

## Consistency mechanisms

Core (build now): supersede-with-reason (skill step 2 + store semantics),
structured/validated writes (Feature 1), focused context (the strategist reads
the blackboard, not the transcript), and re-spawn per boundary with continuity
living in `audit_state`.

Optional (phase-2, do **not** build in v1, per 01 §Consistency): multi-sample
self-consistency or a critic pass, added only if a single strategist pass proves
inconsistent on the target local model.

## Acceptance criteria

- `strix/skills/reconnaissance/audit_strategy.md` exists with valid frontmatter
  and resolves via `load_skills(["audit_strategy"])`; referenced from
  `skills/README.md`.
- `evaluate_qa_gaps` emits a finish-blocking gap for an open **high** lead, and
  no gap once the lead is `done`/`dropped`, or when it is `medium`/`low`
  (covered offline, no LLM).
- The lead-gap `reason`/`suggested_action` is `_scrub_gap`-scrubbed identically
  to every other gap before the review is persisted (testable: a lead whose text
  contains a scrub-matched token is redacted in the assembled review).
- `_build_review_context` surfaces audit signals into `signal_text` and carries
  `_audit_leads` (ids/enums only) (covered offline, mirroring
  `tests/test_loot_store.py::test_qa_loop_surfaces_loot_signals` — sync call, no
  `await`).
- The root convention lives in `strix/skills/coordination/root_agent.md`
  (mentions "audit_strategy"/"Audit Strategist"), covered by a test mirroring
  `test_finish_scan_guards.py::test_root_agent_skill_mentions_review_before_finish`;
  the shared read-`get_audit_state`-before-new-surface line is in
  `system_prompt.jinja`.
- No new tool is registered for Feature 2 (`select_tools` unchanged); the
  strategist uses `create_agent` + existing blackboard tools.
- Live behaviour (strategist actually revises the thesis and the root re-steers)
  is **manual smoke only** — not part of the automated DoD.
