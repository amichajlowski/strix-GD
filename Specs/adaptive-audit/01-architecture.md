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
   specialist child ends ─► on_agent_end hook (deterministic, single-flight)
                                     │ triggers
                                     ▼
                       ┌──────────────────────────┐
                       │ Reflection step (code)    │  one structured LLM call
                       │  strix/core/reflection.py │  on the run's own model,
                       │  reads blackboard,        │  no tools, no agent
                       │  writes thesis            │
                       └───┬──────────────▲────────┘
        reads snapshot     │              │ writes (pure store helpers,
   ┌───────────────────────┼──────────────┼───  supersede-with-reason)
   ▼        ▼        ▼      ▼              ▼
 loot   notes    target_profile     ┌──────────────────┐
 (refs) (findings) + traffic +      │ audit_state (NEW) │
        current QA gaps             │ thesis·assumptions│
   the blackboard (shared/persisted)│ ·leads            │
                                    └────────▲─────────┘
                    ┌────────────────────────┼───────────────────────┐
                    │ Root agent (sole authority)                     │
                    │ reads get_audit_state, acts (spawn/stop/        │
                    │ interrupt, mark leads done/dropped), finish gate│
                    └─────────────────────────────────────────────────┘
```

## Roles (authority stays singular)

- **Reflection step** — analysis only. A **dedicated code path**
  (`strix/core/reflection.py`), not an agent: triggered deterministically when a
  specialist child finishes, it assembles a focused blackboard snapshot in code,
  makes **one structured LLM call on the run's own model**, and writes the
  revised thesis/assumptions/leads into `audit_state` via the pure store helpers.
  It has **no tools** — so structurally it cannot spawn, test, interrupt, or file
  reports (single authority is enforced by construction, not by prompt). Building
  the snapshot in code keeps deep analysis **out of the root's context** (the
  anti-context-rot win) and gives a bounded, controllable input — the reason this
  is a code path and not a strategist child agent (see
  [05-review-findings.md](05-review-findings.md) §0.1).
- **Root** — the only actor. Reads the refreshed `audit_state`, decides, and
  executes: retask/spawn/stop children, interrupt a running child, mark leads
  `done`/`dropped`, and hold the finish gate.
- **Children** — do the testing, write discoveries to the blackboard, and
  **re-read `audit_state` before starting a new surface** (prompt convention).

## Trigger & cadence

**Cadence (a quality decision, not a cost one):** reflect at **boundaries** — a
child agent completing, or a significant event (new confirmed finding, a
`traffic_health` regime change, a WAF/scope surprise). **Never per raw tool
call** — reflecting on partial mid-batch state produces flip-flopping, less
consistent steering.

**Primary mechanism (v1 — deterministic, no new tool, no agent):** a lifecycle
hook fires the reflection when a specialist child finishes. `RunHooks` (already
subclassed by `ReportUsageHooks` in `strix/core/hooks.py`, passed to every agent)
exposes `on_agent_end`. Add `on_agent_end` handling that, for a **child** end
(`parent_id is not None`), schedules `reflection.run_reflection(...)`. This does
not depend on the model remembering to do anything — it fires by construction.
See 03 for the hook body, single-flight coalescing, and budget handling.

- **Single-flight + coalesce:** a burst of near-simultaneous child completions
  (parallel specialists) collapses to one running reflection + at most one
  re-run, via an `asyncio.Lock` + `dirty` flag. No per-completion thrash.
- **Non-blocking:** scheduled as a task so the completing child's teardown isn't
  blocked; the root sees the update on its next `get_audit_state`.

**Deterministic backstop (finish gate):** the **finish gate cannot pass while
high-priority leads are unpursued.** Extend `evaluate_qa_gaps` with an
`audit_state` rule (see 03): open `priority=high` leads become a finish-blocking
gap, deferrable via the existing `acknowledged_gaps` path or by marking the lead
`done`/`dropped`.

> **Scope of the backstop: deep scans only.** The QA gate is gated on
> `qa_loop_enabled == (scan_mode == "deep")` (`runner.py` ~250;
> `finish/tool.py::_qa_review_blocker` returns early otherwise). On quick/standard
> scans the reflection still runs and writes the thesis, but nothing blocks
> finish on unpursued leads. Also: the trigger is **child-completion-keyed**, so a
> run where the root does everything itself with no child agents auto-reflects
> only at the finish gate. Both are acceptable for v1 (the loop targets
> multi-agent deep audits) — state them, don't paper over them.

> **Deferred (phase-2, do NOT build in v1):** (a) multi-sample self-consistency
> or a critic pass over the reflection, if a single pass proves inconsistent on
> the target local model; (b) additional event triggers beyond child-completion
> (e.g. a `traffic_health` regime change) via `on_tool_end`. The v1
> on_agent_end trigger + finish-gate backstop is enough to start.

## Propagation paths

- **In (findings → reflection):** passive. The reflection step *reads* the
  blackboard directly each run (the `loot` / `target_profile` / `audit_state` /
  `notes` module dicts, plus current `evaluate_qa_gaps` output, plus a traffic
  digest when a Caido client is available). Because state is shared+persisted, it
  sees everything every child wrote, across resume. No per-finding wiring.
- **Out (thesis → the audit):** two paths.
  - *Passive:* the revised `audit_state` is on the blackboard; children re-read
    it before new surface; the root reads `get_audit_state` after a reflection
    lands.
  - *Active:* for urgent steers the **root** interrupts a running child via the
    existing `send_message_to_agent` (`interrupt_on_message`) — e.g. "WAF now
    present, switch to evasion." Only the root does this; the reflection step has
    no tools and cannot act.

## Consistency mechanisms (the quality levers)

Cost is not a constraint (local models), so lean on quality/consistency, but
keep the *core* lean:

1. **Supersede-with-reason (keystone, core).** The reflection reads the prior
   thesis and must express a change as a supersede (old assumption marked
   `superseded`, new one links `supersedes: <id>` with a `reason` and updated
   `confidence`) — never a blind rebuild. `audit_state` is append-with-supersede,
   so steering is one continuous line, not independent snapshots that thrash.
2. **Structured, validated, tolerantly-parsed output (core).** The reflection
   requests structured JSON on the run's own model; `apply_reflection` validates
   shape/enums and bounds every field (like `loot`/`target_profile`). Local
   models vary in JSON-schema support, so parsing is tolerant with one retry and
   a clean skip on failure (see 03) — never a crash, never free-form mush in the
   store.
3. **Focused context (core).** The snapshot is assembled in code from the
   blackboard + prior thesis — not inherited from the root's transcript — so the
   input is small and clean (which local models handle markedly better) and
   continuity lives in `audit_state`. This is *why* it's a code path, not a child
   agent (a child would inherit the root's bloated context).
4. **Self-consistency / critic pass (OPTIONAL, phase-2 — documented, not core).**
   If a single reflection proves inconsistent on the target local model,
   run the analysis N times and reconcile, or add a critic pass ("does this
   contradict a prior decision? is each change evidence-backed?") before the
   thesis is committed. Ship the single structured pass first; add this only if
   measured inconsistency justifies it. **Do not build it in v1.**

## Module layout to create

```text
strix/tools/audit_state/__init__.py
strix/tools/audit_state/tools.py    # get/update_audit_state, hydrate, pure helpers, qa_audit_summary
strix/core/reflection.py            # build_reflection_input, apply_reflection, run_reflection (Feature 2)
tests/test_audit_state.py
tests/test_reflection_loop.py
```

Feature 2 adds the `reflection.py` code path + an `on_agent_end` handler in
`strix/core/hooks.py` + a rule in `strix/tools/qa_loop/rules.py` + the
`qa_audit_summary` wiring in `_build_review_context`. **No new tool, no new
agent, no `skills/` file** — the reflection is code, not an agent, so there is
nothing to spawn and no skill to load.

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

`get_audit_state` / `update_audit_state` go in `_BASE_TOOLS` (~333–366).
`select_tools` needs **no change** — no new root-only tool.

> **Scope note.** The reflection code path writes `audit_state` via the pure
> store helpers directly (not via the tool). The `update_audit_state` **tool** is
> for agents: mainly the **root** marking leads `done`/`dropped` (and optionally
> a manual thesis touch-up) so the finish gate can clear. `get_audit_state` on
> every agent is the point — children read the thesis before new surface. Base-tool
> cost: +2 tools; keep both docstrings terse.

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

### 3. Reflection trigger + model call

- **Trigger — `strix/core/hooks.py`.** `ReportUsageHooks(RunHooks)` already
  implements `on_llm_end` (~line 39) and is passed to every agent
  (`spawn_child_agent(..., hooks=hooks)` in `runner.py` ~228–240, and to the root
  run). Add an `on_agent_end(self, context, agent, output)` method (verified
  present on `RunHooks`) that, for a **child** end (`context.context["parent_id"]
  is not None`) and when `coordinator.budget_stopped` is False, schedules
  `reflection.run_reflection(...)` (single-flight — see 03). `on_agent_end` also
  fires for the root; skip it (the scan is ending).
- **Model call — `strix/core/reflection.py`.** Call the run's own model directly
  via `litellm.acompletion(model=<STRIX_LLM>, messages=..., response_format=...)`.
  litellm is already a dependency and globally configured in
  `strix/config/models.py` (`set_default_openai_key`, api_base, litellm defaults),
  and the model id comes from settings (`STRIX_LLM`, `config/settings.py:23`).
  Resolve the model id the same way the run does (pass it in from the hook
  context / config). Record the returned usage into `report_state` (mirror
  `ReportUsageHooks.on_llm_end`'s `record_sdk_usage`) so reflection tokens count
  against `--max-budget-usd`.
- **Blackboard reads** are direct module-dict access (`loot`, `target_profile`,
  `audit_state`, `notes`) + `evaluate_qa_gaps(...)`; a traffic digest is included
  only when a Caido client is in the hook context. No tool calls, no agent.

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

- **Root line → `strix/skills/coordination/root_agent.md`** (extend its existing
  "Pre-finish QA review" section — the exact analog): the audit thesis is
  refreshed **automatically** after each specialist finishes; read
  `get_audit_state`, act on its high-priority leads (spawn/interrupt as needed),
  and mark leads `done`/`dropped` via `update_audit_state` as you resolve them so
  the finish gate can clear (see 03 for exact wording). **No manual spawn** — the
  root does not launch a strategist. **Do not** add an `{% if is_root %}` block to
  the jinja — the template isn't given `is_root`.
- **Shared line → `system_prompt.jinja`** (`EFFICIENCY TACTICS` or
  `<environment>`): "read `get_audit_state` before starting a new surface and
  align to its current high-priority leads." (This region genuinely exists and
  applies to all agents.)

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
  reflection-prompt convention; the store cannot know a string is a secret. So
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
  lands in `.state/` and rides along in any debug bundle; `update_audit_state`
  args (from the root/children) land in the run transcript
  (`run.json`/`agents.db`/`strix.log`). The reflection's own model call is direct
  litellm (not an SDK tool call), so its prompt/response are **not** in the tool
  transcript — but its inputs are the same blackboard, and the write-time scrub
  applies. Because we forbid raw values in `audit_state`, this is derived-intel
  exposure, not secret exposure — but any bundle/export step must still treat
  `.state/` and the transcript as in scope (consistent with Tool Awareness
  01/03).
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
- **The reflection is far more unit-testable than a strategist agent would be**
  (a key win of the code-path vehicle): `build_reflection_input` (pure — snapshot
  → messages) and `apply_reflection` (pure — delta → audit_state, ignores
  malformed) are tested directly with plain dicts; `run_reflection` is tested with
  a **mocked litellm** (assert: tolerant parse + one retry + clean skip on
  failure; usage recorded; skip when `budget_stopped`). The `on_agent_end`
  trigger + single-flight coalescing are tested with a fake coordinator/hook (a
  child end triggers; a root end does not; a burst runs at most one extra). **No
  live model needed for any of this.** Only the end-to-end "model actually revises
  the thesis and the root re-steers" is manual smoke (see 04).
