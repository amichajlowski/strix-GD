# Feature 2 — Reflection Loop (dedicated step)

**Priority: build second. Depends on Feature 1 (audit state store).**

## What it does

At each boundary a **reflection step** reads the blackboard, reconciles it
against the prior thesis, and writes the revised thesis / assumptions / leads
into `audit_state`. The root then reads `audit_state` and steers. A QA-gate rule
makes the finish gate block while high-priority leads are unpursued.

**The reflection is a dedicated code path, not a spawned agent.** It is
triggered deterministically by a lifecycle hook, assembles a focused context in
code, makes **one structured LLM call on the run's own model**, and applies the
result to `audit_state` via the pure store helpers. This vehicle was chosen (over
a strategist child agent) because a full child agent fought the concurrency cap,
inherited the root's bloated context, held the full offensive toolset, and
self-triggered — see [05-review-findings.md](05-review-findings.md) §0.1. A code
path has none of those failure modes and is deterministic and unit-testable.

## Module to create: `strix/core/reflection.py`

Pure, testable core + one impure runner:

```python
def build_reflection_input(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Pure. Assemble the chat messages for the reflection call from a
    blackboard snapshot (prior audit_state, loot refs, target profile, recent
    traffic digest, findings-note titles, current QA gaps). No I/O."""

def apply_reflection(result: dict[str, Any]) -> dict[str, Any]:
    """Pure. Validate the model's structured output and apply it to audit_state
    via the Feature-1 helpers (_apply_assumption / _apply_lead / _update_lead /
    set thesis). Returns a summary of what changed. Ignores malformed items
    rather than raising."""

async def run_reflection(*, model: str, caido_client: Any | None) -> dict[str, Any]:
    """Impure. Snapshot the blackboard, build input, call the model once
    (structured), parse tolerantly, apply, persist. Budget-aware, single-flight
    (see below). Never raises into the caller."""
```

The reflection reads the persisted blackboard **through the existing
lock-protected accessors** (`qa_loot_summary`-style summaries / the store
`_get_*_impl` / a snapshot taken under each store's lock) — **not** by
raw-iterating the module dicts, which would hit `RuntimeError: dict changed size
during iteration` when an agent writes concurrently. It also reads
`evaluate_qa_gaps(...)`. No tool calls. A traffic digest is included **only if** a
Caido client is available from the triggering context; otherwise omitted (the
reflection must work without Caido).

`apply_reflection` writes `audit_state` via the Feature-1 pure helpers **while
holding `_audit_state_lock`** (the pure helpers don't lock; the lock lives in the
tool `_impl`). This serialises it against the root's `update_audit_state` tool
calls and its own `_persist`.

## Deterministic trigger — `on_agent_end` (reuse the existing hooks)

`RunHooks` (already subclassed by `ReportUsageHooks` in `strix/core/hooks.py`,
passed to every agent via `start_child_agent`) exposes `on_agent_end`. Add
`on_agent_end` handling (in `ReportUsageHooks` or a sibling `RunHooks` combined
with it) that fires the reflection when a **specialist child** finishes:

```python
async def on_agent_end(self, context, agent, output) -> None:
    try:                            # MUST never raise into the caller (teardown path)
        ctx = context.context if isinstance(context.context, dict) else {}
        if ctx.get("agent_id") is None or ctx.get("parent_id") is None:
            return                  # root ending = scan over; don't reflect
        coordinator = ctx.get("coordinator")
        if coordinator is not None and (
            getattr(coordinator, "budget_stopped", False)
            or getattr(coordinator, "is_shutting_down", False)
        ):
            return                  # respect budget stop / shutdown
        model = <settings STRIX_LLM>   # StrixProvider().get_model() handles routing
        _schedule_reflection(model=model, caido_client=ctx.get("caido_client"))
    except Exception:               # noqa: BLE001 — a reflection bug must not crash an agent
        logger.exception("on_agent_end reflection scheduling failed")
```

`_schedule_reflection` starts the reflection as a background task
(`asyncio.create_task`) with a done-callback that logs any exception (so it is
never an unretrieved-task error and never propagates). Single-flight: if one is
running, set `dirty` and return; re-run once on completion if `dirty`.

- **Filter:** only child agents (`parent_id is not None`) trigger it — the root
  ending means the scan is over. There is **no strategist agent** to exclude
  (the old self-trigger bug, F1, cannot occur — the reflection is not an agent).
- **Single-flight + coalesce:** `_schedule_reflection` holds an `asyncio.Lock`.
  If a reflection is already running, it sets a `dirty` flag instead of queuing a
  second; when the running one finishes it re-runs once if `dirty`. This collapses
  a burst of near-simultaneous child completions (parallel specialists) into one
  extra reflection, not N — the concurrency thrash a per-completion spawn would
  cause.
- **Non-blocking:** schedule as a task (`asyncio.create_task`) so the completing
  child's teardown is not blocked. The root reads the updated `audit_state` on
  its next `get_audit_state`.

> **Scope limits (state them, don't fix in v1):**
> - **Deep scans only** for the finish-gate backstop (`qa_loop_enabled ==
>   (scan_mode == "deep")`). On quick/standard the reflection still runs and
>   writes the thesis, but nothing blocks finish on unpursued leads.
> - **Child-completion-keyed.** A run where the root does everything itself with
>   no child agents never fires `on_agent_end` for a child, so it auto-reflects
>   only via the finish gate. Acceptable — the loop targets multi-agent audits.

## The reflection prompt

Keep the instruction template in `strix/core/reflection.py` (a module constant)
or a co-located `reflection_prompt.md` loaded by it — **not** a `skills/` file
(there is no agent to load a skill). It instructs the model to, given the
snapshot: reconcile against the prior thesis (supersede changed assumptions with
a reason, don't rebuild), revise the one-paragraph thesis, add/repriotise leads,
**reference loot by `loot_id` never by value**, and return **structured JSON**
matching the `apply_reflection` schema (thesis, assumptions[], leads[],
lead_updates[]). Low temperature. If nothing material changed, return an empty
delta.

## Model call, resolution, and structured output — mirror `report/dedupe.py`

Do **not** hand-roll a litellm call or a model-name mapping. Strix already makes
a standalone one-shot model call outside the Runner in
`strix/report/dedupe.py:189–214`; copy it:

- `configure_sdk_model_defaults(settings)`, then
  `model = StrixProvider().get_model(resolved_model)` — `resolved_model` is the
  settings `STRIX_LLM`. `get_model` applies the run's routing (`ollama/X` →
  `ollama_chat/X`, etc.), so there is **nothing to map by hand** — this is why the
  reflection uses the same model the audit uses, on any provider.
- `resp = await model.get_response(system_instructions=<reflection prompt>,
  input=<snapshot JSON>, model_settings=ModelSettings(retry=..., include_usage=
  True), tools=[], output_schema=None, handoffs=[], tracing=DISABLED, ...)`.
- **Structured output the robust way:** like dedupe, instruct "respond with ONLY
  the JSON object" in the prompt and parse the text tolerantly (extract the first
  JSON object, tolerate prose) — do **not** rely on `response_format`/JSON-schema
  (uneven on local models). On parse failure **retry once**; on a second failure
  log and **skip** (leave `audit_state` unchanged) — never crash.

## Budget accounting

`get_response` returns an SDK response with `.usage`, so record it exactly as
dedupe does: `report_state.record_sdk_usage(agent_id="reflection",
usage=resp.usage, model=resolved_model)`. That feeds `_total_cost` →
`get_total_llm_cost()` → the `on_llm_end` budget check, so reflection spend
enforces `--max-budget-usd` (caught on the next model turn). Skip the whole
reflection if `coordinator.budget_stopped` is already set.

## QA-gate integration (the enforcement)

- `qa_loop/tool.py::_build_review_context` — add `audit = qa_audit_summary(...)`,
  extend `signal_text` with `audit["signals"]`, carry `audit["refs"]` as
  `_audit_leads` (**ids/enums only** — see 01/02). Mirror the `qa_loot_summary`
  wiring.
- `qa_loop/rules.py::evaluate_qa_gaps` — add one rule: every open
  `priority=high` lead (`status=open`) → a **high** finish-blocking gap
  *"Pursue or explicitly defer high-priority lead: `<text>`."* **Deep scans
  only.** This is the first QA rule with a **dynamic** field in `reason`; it must
  flow through the existing `_scrub_gap`/`_scrub_text` path before the review is
  persisted.
- **Reuse `acknowledged_gaps` for "explicitly defer".** Lead-gaps are ordinary
  gaps with a deterministic `gap_id`; the root defers a lead by acknowledging its
  `gap_id` (the existing `assemble_review(acknowledged_gaps=...)` path) or by
  setting the lead `status=dropped`/`done`. This prevents finish-gate **livelock**
  when new high leads keep appearing.

## Propagation

- **In:** the reflection reads the shared blackboard directly (module dicts +
  current QA gaps). Sees everything every child wrote, across resume.
- **Out (passive):** the revised `audit_state` is on the blackboard; every agent
  reads `get_audit_state` before a new surface; the root reads it after a
  reflection lands.
- **Out (active):** the **root** interrupts a running child via the existing
  `send_message_to_agent` for urgent steers. The reflection step never acts —
  it only writes the thesis. Single authority (root) is preserved structurally
  (the reflection has no tools).

## Prompt wiring (agents must use the thesis)

- **Shared line → `system_prompt.jinja`** (`EFFICIENCY TACTICS` or
  `<environment>`): "read `get_audit_state` before starting a new surface and
  align to its current high-priority leads."
- **Root line → `strix/skills/coordination/root_agent.md`** (root-only guidance
  lives there, not in the jinja — it has no `is_root` conditional): "the audit
  thesis is refreshed automatically after each specialist finishes; read
  `get_audit_state`, act on its high-priority leads (spawn/interrupt as needed),
  and mark leads `done`/`dropped` via `update_audit_state` as you resolve them so
  the finish gate can clear."
- **No spawn convention** — the loop fires automatically; the root does not
  spawn a strategist.

## Consistency mechanisms

Core (build now): supersede-with-reason (prompt + store semantics), structured +
tolerantly-parsed output, focused context (assembled in code, not inherited),
low temperature, single-flight coalescing. Continuity lives in `audit_state`.

Optional (phase-2, do **not** build in v1): multi-sample self-consistency or a
critic pass, added only if a single reflection proves inconsistent on the target
local model. Also phase-2: event triggers beyond child-completion (e.g. a
`traffic_health` regime change) via `on_tool_end`.

## Acceptance criteria

- `build_reflection_input` is pure and covered (given a snapshot → messages
  including prior thesis, loot refs by id, current QA gaps; no raw values).
- `apply_reflection` applies a well-formed delta to `audit_state` (thesis +
  supersede + leads) and **ignores malformed items without raising**; covered.
- `run_reflection` skips cleanly (no change, no raise) on a parse failure after
  one retry, and when `budget_stopped` is set (both covered with a mocked
  `model.get_response` — no live model).
- `run_reflection` reads via the lock-protected accessors (does not raw-iterate
  the store dicts) and applies under `_audit_state_lock`.
- The `on_agent_end` trigger fires the reflection for a **child** end and **not**
  for a root end; single-flight coalescing runs at most one extra reflection for
  a burst; **the hook never raises** even if the scheduled reflection blows up
  (covered with a fake coordinator/hook, no live model).
- Reflection usage is recorded via `record_sdk_usage(usage=resp.usage)` (like
  `report/dedupe.py`) and reaches `get_total_llm_cost()` (covered with a mocked
  `model.get_response` returning a usage object).
- **Regression guard:** `_build_review_context` + `evaluate_qa_gaps` run cleanly
  and block nothing when `audit_state` is **empty** (feature unused) — the
  unguarded finish path must not break for non-adaptive scans.
- `evaluate_qa_gaps` emits a finish-blocking gap for an open **high** lead; none
  for `done`/`dropped`/`medium`/`low`; the gap `reason` is `_scrub_gap`-scrubbed;
  an acknowledged lead-gap no longer blocks. All offline.
- `_build_review_context` surfaces audit signals into `signal_text` and carries
  `_audit_leads` (ids/enums only). Offline, mirrors
  `tests/test_loot_store.py::test_qa_loop_surfaces_loot_signals` (sync — no
  `await`).
- Prompt wiring present: shared line in `system_prompt.jinja`; root line in
  `root_agent.md`.
- **Live behaviour** (the model actually revises the thesis and the root
  re-steers) is manual smoke only — not part of the automated DoD.
