# Architecture & Wiring

## How this fits the existing system

Strix runs a **root agent** (orchestrator: spawns/stops children, holds the
`finish_scan` + QA gate) and **child specialists** (spawned via `create_agent`,
finish via `agent_finish`). All agents share four persisted, run-wide stores —
`notes`, `todos`, `loot`, `target_profile` — hydrated on resume and visible to
every agent. That shared state is the **blackboard**.

The only findings→steering today is the **QA gap engine**
(`qa_loop/rules.py::evaluate_qa_gaps`): *deterministic rules over discovered
signals*, surfaced to the root via the `review_before_finish` tool — but only at
the finish gate. There is no mid-audit synthesis and no place for an evolving
theory of the target to live.

This feature adds that layer with **one new store** and **one delegated
reflection loop**, reusing the child-spawn path and the QA-gate machinery.

```
                    ┌───────────────────────────────────────────────┐
                    │  Root agent (orchestrator + sole authority)     │
                    │  spawn/stop/interrupt children · finish gate    │
                    └───┬───────────────────────────────▲────────────┘
      at a boundary,    │ 1. create_agent(               │ 4. read revised
      root spawns       ▼    skills=["audit_strategy"])  │    thesis+leads via
      the strategist   ┌──────────────────────┐         │    get_audit_state,
                       │ Strategist (delegated)│         │    act (retask,
                       │  audit_strategy skill │         │    spawn, interrupt)
                       │  reads blackboard,    │─────────┘
                       │  writes thesis        │ 3. update_audit_state
                       └───┬──────────▲────────┘   (supersede-with-reason)
              2. read      │          │
        ┌──────────────────┼──────────┼──────────────────────────┐
        ▼                  ▼          ▼                           ▼
   ┌─────────┐      ┌──────────┐ ┌───────────────┐      ┌──────────────────┐
   │ loot    │      │ notes    │ │ target_profile│      │ audit_state (NEW) │
   │         │      │(findings)│ │ + traffic     │      │ thesis·assumptions│
   └─────────┘      └──────────┘ └───────────────┘      │ ·leads (blackboard)│
        the blackboard (shared, persisted, all-agent-visible)   └──────────────────┘
```

## Roles (authority stays singular)

- **Strategist** — analysis only. A normal Strix child agent spawned through the
  existing path with a dedicated `audit_strategy` skill and a narrow task:
  *read the blackboard, reconcile it against the prior thesis, write the revised
  thesis/assumptions/leads into `audit_state`.* It **never** spawns, stops, or
  interrupts agents, and never files vuln reports. Its "output" is its
  structured writes to `audit_state` — exactly as recon's output is its writes
  to `target_profile`. Delegating it keeps deep analysis **out of the root's
  context** (the anti-context-rot win).
- **Root** — the only actor. Reads the revised `audit_state`, decides, and
  executes: retask/spawn/stop children, interrupt a running child, and hold the
  finish gate.
- **Children** — do the testing, write discoveries to the blackboard, and
  **re-read `audit_state` before starting a new surface** (prompt convention).

## Trigger & cadence

**Cadence (a quality decision, not a cost one):** reflect at **boundaries** — a
child agent completing, or a significant event (new confirmed finding, a
`traffic_health` regime change, a WAF/scope surprise). **Never per raw tool
call** — reflecting on partial mid-batch state produces flip-flopping, less
consistent steering.

**Primary mechanism (v1 — reuse `create_agent`, add no new tool):** the root
spawns the strategist through the **existing** `create_agent(name="Audit
Strategist", skills=["audit_strategy"], ...)` path, waits for it with
`wait_for_message`, then reads `get_audit_state`. The **root convention** (in
`strix/skills/coordination/root_agent.md` — see §5) requires: *after every
`wait_for_message` that returns a completed child, spawn the audit strategist and
read `get_audit_state` before dispatching new work.* No bespoke reflection tool —
it would only wrap `create_agent` + `wait_for_message` + `get_audit_state`, which
all exist.

**Deterministic backstop (this is what makes it consistent, not the prompt):**
the **finish gate cannot pass while high-priority leads are unpursued.** Extend
`evaluate_qa_gaps` with an `audit_state` rule (see 03): open `priority=high`
leads with `status=open` become a finish-blocking gap. So even if the root skips
a mid-audit reflection, the audit cannot *finish* without reconciling the
thesis. Consistency is enforced by the gate, not by the model remembering.

> **Scope of the backstop: deep scans only.** The QA gate is gated on
> `qa_loop_enabled`, which is `scan_mode == "deep"` (`runner.py` ~250;
> `finish/tool.py::_qa_review_blocker` returns early otherwise). On
> quick/standard scans the loop degrades to **prompt-convention-only** (no
> deterministic backstop). This is acceptable — non-deep scans are shallow by
> design — but state it; do not claim the audit "cannot complete un-reconciled"
> unconditionally.

> **Deferred (phase-2, do NOT build in v1), in ascending order of plumbing:**
> (a) a thin root-only `review_findings` convenience tool that wraps
> spawn→wait→return-leads with a debounce, *if* freehand orchestration proves
> unreliable on the target local model; (b) a fully deterministic trigger that
> auto-runs the strategist from the child-completion seam in
> `strix/core/execution.py` / the coordinator status transition — more consistent
> still, but touches the execution loop. The v1 prompt-convention +
> finish-gate backstop makes both unnecessary to start. Naming them keeps v1
> lean without losing the upgrade path.

## Propagation paths

- **In (findings → strategist):** passive. The strategist *reads* the blackboard
  each run (`get_loot`, `get_target_profile`, `traffic_health`, `list_notes`,
  `get_audit_state`). Because state is shared+persisted, it sees everything every
  child wrote, across resume. No per-finding wiring.
- **Out (thesis → the audit):** two paths.
  - *Passive:* the revised `audit_state` is on the blackboard; children re-read
    it before new surface; the root reads `get_audit_state` after the strategist
    finishes.
  - *Active:* for urgent steers the **root** interrupts a running child via the
    existing `send_message_to_agent` (`interrupt_on_message`) — e.g. "WAF now
    present, switch to evasion." Only the root does this.

## Consistency mechanisms (the quality levers)

Cost is not a constraint (local models), so lean on quality/consistency, but
keep the *core* lean:

1. **Supersede-with-reason (keystone, core).** The strategist reads the prior
   thesis and must express a change as a supersede (old assumption/lead marked
   `superseded`, new one links `supersedes: <id>` with a `reason` and updated
   `confidence`) — never a blind rebuild. `audit_state` is append-with-supersede,
   so steering is one continuous line, not independent snapshots that thrash.
2. **Structured writes (core).** `update_audit_state` validates shape/enums and
   bounds every field (like `loot`/`target_profile`), so a weaker local model
   cannot drift the thesis into free-form mush.
3. **Focused context (core).** The strategist reads only the blackboard + prior
   thesis, not the whole run transcript — smaller, cleaner context, which local
   models handle markedly better, and continuity lives in `audit_state` rather
   than the strategist's own history (so re-spawn per boundary is fine).
4. **Self-consistency / critic pass (OPTIONAL, phase-2 — documented, not core).**
   If a single strategist pass proves inconsistent on the target local model,
   run the analysis N times and reconcile, or add a critic pass ("does this
   contradict a prior decision? is each change evidence-backed?") before the
   thesis is committed. Ship the single structured pass first; add this only if
   measured inconsistency justifies it. **Do not build it in v1.**

## Module layout to create

```text
strix/tools/audit_state/__init__.py
strix/tools/audit_state/tools.py          # get/update_audit_state, hydrate, pure helpers, qa_audit_summary
strix/skills/reconnaissance/audit_strategy.md   # the strategist skill (filename stem = audit_strategy)
tests/test_audit_state.py
tests/test_strategist_loop.py
```

No new tool module for Feature 2: it is the `audit_strategy` skill + a rule
added to the existing `strix/tools/qa_loop/rules.py` + the wiring in
`_build_review_context`. The strategist is spawned via the existing
`create_agent`.

## Exact wiring anchors

Verified against the current tree; line numbers approximate — match on
surrounding code.

### 1. Tool registration — `strix/agents/factory.py`

Imports live ~29–56 (loot/notes/proxy/target_profile blocks). Add:

```python
from strix.tools.audit_state.tools import (   # NEW
    get_audit_state,
    update_audit_state,
)
```

`get_audit_state` / `update_audit_state` go in `_BASE_TOOLS` (~333–366) — every
agent reads the thesis; the strategist writes it. `select_tools` needs **no
change** — no new root-only tool in v1 (the strategist is spawned via the
existing `create_agent`).

> **Scope note.** `get_audit_state` on every agent is the point (children must
> read the thesis before new surface). `update_audit_state` on every agent is
> acceptable but the *intended* writer is the strategist; keep its docstring
> explicit ("normally only the strategist writes this"). Do not add a separate
> role/toolset mechanism in v1 — prompt-scoping via the `audit_strategy` skill
> is enough (see non-goals). Base-tool cost: +2 tools (`get_audit_state`,
> `update_audit_state`); keep both docstrings terse.

### 2. State hydration — `strix/core/runner.py`

Next to the existing notes/todos/loot/target_profile hydration (~108–120). The
existing hydration imports are **function-local** inside `run_strix_scan` (which
is why `runner.py` carries a `PLC0415` ignore) — add the new import in that same
local block, not at module top:

```python
from strix.tools.audit_state.tools import hydrate_audit_state_from_disk   # NEW (local import)
...
hydrate_audit_state_from_disk(state_dir)                                   # NEW
```

### 3. Strategist spawn — reuse existing child machinery

- `create_agent(ctx, name, task, inherit_context, skills)`
  (`agents_graph/tools.py` ~432) — the spawn path; `skills=["audit_strategy"]`.
- `spawn_child_agent` / `start_child_agent` (wired in `runner.py` ~220–240) —
  the machinery `create_agent` routes through; the root's `create_agent` call
  launches the strategist through it. No direct use needed by this feature.
- `send_message_to_agent` / `wait_for_message` (`agents_graph/tools.py` ~124 /
  ~288) — messaging a long-lived strategist (alternative to re-spawn) and
  root→child interrupts.
- `build_strix_agent(is_root=False, skills=...)` (`factory.py` ~378) — children
  get `select_tools(is_root=False)`, i.e. `_BASE_TOOLS + agent_finish`, so the
  strategist already has every blackboard read tool plus `update_audit_state`.

### 4. QA-gate integration — `strix/tools/qa_loop/`

- `_build_review_context` (`tool.py` ~99–133) — add `audit = qa_audit_summary()`,
  extend `signal_text` with its `signals`, carry its `refs` as `_audit_leads`
  (mirror the `qa_notes_summary` / `qa_loot_summary` wiring the Tool Awareness
  build added). **`refs` must be ids/enums only (no free text) — see Secret
  discipline.**
- `evaluate_qa_gaps` (`rules.py` ~362) — add a small rule: open `priority=high`
  leads → a finish-blocking gap ("pursue or explicitly defer lead X"). This is
  the deterministic backstop. This is the **first** QA rule to interpolate a
  dynamic field (the lead text) into a gap's `reason`/`suggested_action`; it
  **must** flow through the existing `_scrub_gap`/`_scrub_text` path in
  `qa_loop/tool.py` (which already scrubs `reason`/`evidence`/`suggested_action`)
  before the review is persisted — do not bypass it.

### 5. Prompt discoverability — two files (mind the anchor)

`system_prompt.jinja` has **no `is_root` conditional** (its only conditional is
`{% if interactive %}`; `is_root` is not passed to `render`). Root-only guidance
is delivered by the `strix/skills/coordination/root_agent.md` skill, which
`prompt.py` (~45–46) auto-appends for the root agent only. So:

- **Root-only convention → `strix/skills/coordination/root_agent.md`** (extend
  its existing "Pre-finish QA review" section — the exact analog): after a child
  completes (a `wait_for_message` returns its report), spawn
  `create_agent(name="Audit Strategist", skills=["audit_strategy"])`, wait for
  it, then read `get_audit_state` before dispatching new work (see 03 for exact
  wording). **Do not** try to add an `{% if is_root %}` block to the jinja — the
  template isn't given `is_root`.
- **Shared line → `system_prompt.jinja`** (`EFFICIENCY TACTICS` or
  `<environment>`): "read `get_audit_state` before starting a new surface and
  align to its current leads." (This region genuinely exists and applies to all
  agents.)

### 6. Lint config — `pyproject.toml`

Add the new tool module to `[tool.ruff.lint.per-file-ignores]`, mirroring the
loot/target_profile entries (`RunContextWrapper` must be imported eagerly for
SDK schema generation, so `TC002` must be ignored):

```toml
"strix/tools/audit_state/tools.py" = ["PLC0415", "TC002"]
```

## Secret discipline (read before touching audit_state)

`audit_state` holds **derived** intelligence — hypotheses, assumptions, leads —
that a reasoning step produced from the blackboard. It sits at the same
run-local trust level as `target_profiles.json` (notes-style `0644` atomic
write; **no** raw secrets, so no `0o600` requirement).

- **Never store raw secret values in `audit_state`.** Reference loot by
  `loot_id` (e.g. a lead "reuse credential `ab12cd` against /admin"), never by
  value. If a hint is unavoidable, use `loot.mask_value`.
- **Write-time scrub backstop (cheap defense-in-depth).** "ids only" is a
  strategist-skill convention; the store cannot know a string is a secret. So
  `update_audit_state` runs `scrub_secrets(...)` over its free-text fields
  (`thesis`, `assumption`, `lead`, `reason`) at write time — one call per field,
  the same helper `qa_*_summary` uses at read time. It silently degrades a
  would-be leaked value to `XXXX` without rejecting the write (no new
  validation-failure mode for a weak model to recover from). Do **not** add more
  than this (no write-rejection, no loot cross-referencing) — the store is
  derived-intel; one scrub call is the proportionate belt.
- **Persisted `refs` carry no free text.** `qa_audit_summary`'s `refs` (which
  land in the persisted QA review) carry only ids/enums (`lead_id`/`priority`/
  `status`, `assumption_id`/`confidence`) — exactly like `qa_loot_summary`'s
  `refs`. Free text goes only into the in-memory, never-persisted `signals`
  list. Do not put lead/assumption `text` into `refs`.
- **Same bundle/transcript caveat as `target_profiles.json`.** `audit_state.json`
  lands in `.state/` and rides along in any debug bundle; the strategist's task
  args and `update_audit_state` args land in the run transcript
  (`run.json`/`agents.db`/`strix.log`). Because we forbid raw values in
  `audit_state`, this is derived-intel exposure, not secret exposure — but any
  bundle/export step must still treat `.state/` and the transcript as in scope
  (consistent with Tool Awareness 01/03).
- **`qa_audit_summary`** feeds the QA gate: return bounded, `scrub_secrets`-clean
  `refs`/leads and in-memory `signals` only — never raw values (there should be
  none to leak, but scrub defensively, exactly like `qa_loot_summary`).

## Testing strategy

- **Pure helpers are the unit-test target:** the supersede/revision reconciler,
  lead ordering/status transitions, and `qa_audit_summary` extraction all take
  plain dicts and need no LLM/Docker/network/Caido.
- **Store** tested by calling `_impl` functions + a `_persist`/`hydrate`
  round-trip.
- **QA-gate integration** tested offline exactly like the loot wiring — the
  template is `tests/test_loot_store.py::test_qa_loop_surfaces_loot_signals`
  (note: `_build_review_context` is a **sync** def — call it **without** `await`;
  seed the store via the `_impl` after `hydrate_audit_state_from_disk(tmp_path)`).
  Seed an open high lead, call `_build_review_context` / `evaluate_qa_gaps`,
  assert the blocking gap.
- **The strategist LLM loop is NOT unit-tested** (it needs a live model). Its
  *contract* is tested at the seams: the `audit_strategy` skill resolves via
  `load_skills`, the root convention is present in `root_agent.md`,
  `select_tools(is_root=True)` gains no new tool, and the store/gate behave. Live
  behaviour is manual-smoke only (see 04).
