# 02 — Task List & Tests

> Extends [01-architecture.md](01-architecture.md). TDD: write the test (RED),
> implement (GREEN), refactor. Every task ships with its test in the same PR.
> Verify locally with `make check` (ruff + mypy) and `pytest` before handing off.

Build order is deliberate: **T1 (meter+margin) and T2 (back-off) fix the live
production `400` and stop agents dying** — land them first, standalone. T3–T5
add compaction. T6 thins the root. T7 is the fallback.

Existing tests to mirror for style/fixtures: `tests/` alongside
`test_context_limit*.py`, `test_sessions*.py`, `test_execution*.py` (grep for the
current filenames; follow their fixture patterns and marker usage).

---

## T1 — Honest meter + margin  *(Layer 0)*

**Change** `strix/core/context_limit.py` + `strix/config/settings.py`:
- Count `model_data.instructions` tokens in `ContextLimitFilter.__call__`/`_budget`.
- Percentage reserve: `reserve = max(_RESERVE_TOKENS, ceil(window * reserve_ratio))`.
- Conservative `bytes_per_token` (default 3.5), configurable via settings.
- Budget = `min(configured, learned) − reserve − instruction_tokens`, floored at
  `window // 2`.
- New settings (`llm` section): `reserve_ratio` (0.10), `bytes_per_token` (3.5).

**Tests** (`test_context_limit.py`):
- `test_budget_reserves_instruction_tokens` — a large `instructions` string
  reduces the item budget by ~its token estimate.
- `test_reserve_is_percentage_of_window` — reserve scales with window, floored at
  16 384.
- `test_conservative_estimate_overcounts` — bytes/3.5 ≥ bytes/4 for the same text.
- `test_trims_to_fit_including_instructions` — a history that overflows *only*
  when instructions are counted is trimmed; previously it wasn't.

---

## T2 — Progressive back-off recovery  *(Layer 4, no compaction yet)*

**Change** `strix/core/context_limit.py`: add
`ContextLimitFilter.shrink(factor: float) -> bool` (multiply an internal
`_shrink_factor` applied in `_budget`; return `False` at a floor, e.g. factor
would drop budget below `window*0.25`).

**Change** `strix/core/execution.py` recovery block (~402–439): when
`input_rejected` and `note_context_length(reported_max)` is `False`, call
`context_filter.shrink(0.85)` and `continue` (bounded by the existing
`context_relimits < 3`, raised to e.g. `< 6` to allow a couple of shrinks).

**Tests** (`test_execution.py` / `test_context_limit.py`):
- `test_double_context_400_recovers` — **the regression test for the observed
  bug.** Fake model raises `BadRequestError(status_code=400, message="maximum
  context length is 262144 tokens ...")` twice with the *same* reported max, then
  succeeds. Assert the agent ends `completed`/`stopped`, **not** `failed`, and
  that `shrink` was invoked.
- `test_shrink_has_floor` — repeated `shrink` returns `False` at the floor; the
  loop then escalates (parks only after compaction path in T5 also fails).

---

## T3 — `render_blackboard_index`  *(Layer 2, pure reads)*

**Change** new pure function assembling the bounded pointer index from
`audit_state._snapshot_audit_state`, `notes` metadata listing / `qa_notes_summary`,
`loot.qa_loot_summary`, reporting index, `todo`, `agents_graph` digest.

**Tests** (`test_blackboard_index.py`):
- `test_index_lists_ids_not_bodies` — output contains note/loot/vuln IDs + titles
  but not full note bodies or unmasked loot values.
- `test_index_is_bounded` — with 10 000 synthetic notes the rendered index stays
  under a fixed char/token ceiling (relies on the readers' existing caps).
- `test_index_reads_only` — no store mutation occurs.

---

## T4 — `compact_session`  *(Layer 1 core)*

**Change** `strix/core/sessions.py`: implement `compact_session(...)` per
[01-architecture.md](01-architecture.md) §Layer 1. Reuse the orphan-dropping
helper from `context_limit.py`. The `summarize` callback makes one bounded
structured model call (mirror `reflection.py`); in tests it is injected as a stub.

**Tests** (`test_sessions.py`):
- `test_compaction_pins_task_and_recent` — first item and last `keep_recent`
  items are byte-identical after compaction.
- `test_compaction_inserts_index_marker` — exactly one summary/marker item is
  inserted between task and recent; it contains the blackboard index.
- `test_compaction_preserves_call_output_pairs` — no `function_call` is left
  without its `function_call_output` (and vice-versa) across the boundary.
- `test_compaction_mutates_persisted_session` — after `compact_session`, a fresh
  `get_items()` (reopened session) returns the compacted list → **resume loads the
  compacted history.**
- `test_compaction_noop_when_small` — under target, returns `False`, session
  unchanged.
- `test_compaction_returns_false_when_task_plus_recent_exceed_target` — the
  escalation signal for T2/T7.
- `test_summarize_call_is_bounded` — the stub receives only the `old` span +
  index, never the pinned/recent items twice.

---

## T5 — Wire compaction into the turn loop + recovery  *(Layers 1↔4)*

**Change** `strix/core/execution.py`:
- **Proactive:** a pre-call hook (`on_turn_start` or a check at the top of
  `_run_cycle`) compacts when `est_tokens >= window * compaction_trigger_ratio`.
- **Reactive:** in the recovery block, attempt `compact_session` *before* `shrink`;
  if it changed the session, retry.
- **Last resort:** if compaction returns `False` and `shrink` is floored, inject a
  `render_blackboard_index` "continue from here" turn once before parking.

**Tests** (`test_execution.py`):
- `test_proactive_compaction_fires_at_ratio` — crossing the ratio triggers exactly
  one compaction before the model call.
- `test_recovery_prefers_compaction_before_shrink` — on a size 400, compaction is
  attempted first; shrink only if compaction is a no-op.
- `test_never_parks_on_recoverable_400` — extends T2: even when reported max is
  static, the compaction→shrink→continue chain avoids `failed`.

---

## T6 — Thin root notification cap  *(Layer 3)*

**Change** `strix/tools/agents_graph/tools.py` `agent_finish`: cap the
`result_summary` text appended to the parent context to a bounded digest; store
the full summary as a `note` and reference its ID in the digest.

**Tests** (`test_agents_graph.py`):
- `test_result_summary_ingested_is_capped` — a 50 KB summary is truncated in the
  parent-facing payload; the full text is retrievable via the referenced note ID.
- `test_findings_list_untouched` — the structured `findings` list is not
  truncated (only the narrative prose is).

---

## T7 — `spawn_successor` fallback  *(Layer 5, optional / last)*

**Change** `strix/tools/agents_graph/tools.py` + `sessions.py`: `spawn_successor`
seeds a new same-role agent from `[task, render_blackboard_index, tail]`,
re-points parent/child edges + pending message routing, transfers budget, marks
predecessor `superseded`.

**Tests** (`test_agents_graph.py`):
- `test_successor_inherits_graph_edges` — children of the predecessor become
  children of the successor; pending `send_message_to_agent` retargets.
- `test_successor_seeded_from_index_not_prose` — seed contains the structured
  index, not an LLM summary blob.
- `test_successor_only_when_compaction_exhausted` — not invoked while
  `compact_session` can still reduce.

---

## Definition of done

- [ ] T1 + T2 merged and the **`test_double_context_400_recovers` regression test
      passes** — the exact `_6ed5` failure no longer parks an agent.
- [ ] `compact_session` bounds a long synthetic run below the window across many
      turns (an integration test that loops turns and asserts stored session
      size plateaus).
- [ ] Resume of a compacted session loads the compacted history (no re-overflow).
- [ ] `make check` clean (ruff, mypy); `pytest` green; coverage ≥ 80% on new code.
- [ ] No new runtime dependency; no vector store; child→parent handoff semantics
      unchanged.
- [ ] Docs: note the new `STRIX_LLM_*` knobs in the config sample (mirror the
      `STRIX_LLM_MAX_CONCURRENCY` doc precedent).
