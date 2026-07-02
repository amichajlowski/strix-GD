# Task List & Test Plan

Execute in order. Feature 1 (audit state) ships independently; Feature 2 (the
loop) builds on it. Full behaviour is in the per-feature docs; this is the
checklist + tests. Tests must be pure/offline — no live LLM, Docker, network, or
Caido.

## v1 scope — build exactly these (nothing more)

New files: `strix/tools/audit_state/__init__.py`, `strix/tools/audit_state/tools.py`,
`strix/core/reflection.py`, `tests/test_audit_state.py`,
`tests/test_reflection_loop.py`.
Edits: `strix/agents/factory.py` (imports + `_BASE_TOOLS`, **not** `select_tools`),
`strix/core/runner.py` (hydrate), `strix/core/hooks.py` (`on_agent_end` trigger),
`strix/tools/qa_loop/rules.py` (lead-gap rule),
`strix/tools/qa_loop/tool.py` (`qa_audit_summary` wiring),
`strix/skills/coordination/root_agent.md` (root line: auto-refresh + act on
leads), `strix/agents/prompts/system_prompt.jinja` (shared line),
`pyproject.toml` (per-file-ignore for the audit_state tool module).
Do **not** build: any new tool beyond `get_audit_state`/`update_audit_state`, a
strategist child agent / `audit_strategy` skill, a `review_findings` tool, an
execution-loop trigger, or a self-consistency/critic pass (all phase-2 or
rejected — see 01/05 §0.1).

## Implementation constraints

- Keep changes small and local; do not refactor unrelated code; do not revert
  unrelated working-tree changes.
- Pure helpers hold the logic; tool wrappers only map I/O → JSON.
- Mirror `strix/tools/notes/tools.py` for the store (but it holds one document).
- Follow the secret discipline in [01-architecture.md](01-architecture.md):
  `audit_state` stores **derived intel only**, ids not raw values.
- **Lint gotchas (fail `make lint`, in the DoD):** never name a param/field
  `type` (`A002`); the new tool module needs a `["PLC0415", "TC002"]`
  per-file-ignore in `pyproject.toml` (eager `RunContextWrapper` import). Baseline
  `make lint`/`make type-check` already have pre-existing failures in unrelated
  files (see the Tool Awareness build) — the bar is **zero new** lint/type errors
  from this delta, not a globally green tree.
- `qa_audit_summary` must be **wired** into `qa_loop/tool.py`
  `_build_review_context`, and the lead-gap rule into `evaluate_qa_gaps` — an
  unwired summary/rule is dead code.

---

## Phase 1 — Audit state store

### Task 1.1 — Store module
Create `strix/tools/audit_state/__init__.py` and
`strix/tools/audit_state/tools.py` modelled on notes (single-document variant):
module dict holding the state doc, `_audit_state_lock`, `_audit_state_path`,
`hydrate_audit_state_from_disk`, atomic `_persist`. Pure helpers
`_apply_assumption` (with supersede), `_apply_lead`, `_update_lead`, and
`qa_audit_summary`. Enum validation, bounds, superseded-history cap. Per
[02-audit-state.md](02-audit-state.md).

### Task 1.2 — Tools
`get_audit_state` and `update_audit_state` `@function_tool` wrappers offloading
via `asyncio.to_thread`, flat SDK-clean params per 02. Terse docstrings.

### Task 1.3 — Register + hydrate + prompt + lint config
Add both tools to `_BASE_TOOLS` (`factory.py`); call
`hydrate_audit_state_from_disk(state_dir)` in `runner.py`; add the shared prompt
line (read `get_audit_state` before new surface); add the per-file-ignore in
`pyproject.toml`.

**Acceptance:** see [02-audit-state.md](02-audit-state.md#acceptance-criteria).

---

## Phase 2 — Reflection loop (code path)

### Task 2.1 — Reflection module
Create `strix/core/reflection.py` with the pure `build_reflection_input(snapshot)`
and `apply_reflection(result)` and the impure `run_reflection(*, model,
caido_client)` per [03-strategist-loop.md](03-strategist-loop.md). `run_reflection`
snapshots the blackboard (loot/target_profile/audit_state/notes module dicts +
`evaluate_qa_gaps` + optional traffic digest), calls `litellm.acompletion` on the
run's model with structured output, parses tolerantly (retry once, else skip),
applies via the Feature-1 pure helpers, and records usage into `report_state`.
Single-flight (`asyncio.Lock` + `dirty` flag). Keep the reflection prompt as a
module constant. **No agent, no tools, no `skills/` file.**

### Task 2.2 — Deterministic trigger
Add `on_agent_end` to `strix/core/hooks.py` (`ReportUsageHooks` or a sibling
`RunHooks`) that schedules `run_reflection` for a **child** end
(`parent_id is not None`), skips the root end and `budget_stopped`, and runs
non-blocking (`create_task`). Resolve the model id from settings (`STRIX_LLM`);
pass `caido_client` from the hook context if present.

### Task 2.3 — QA-gate rule + wiring
Add the open-high-lead → finish-blocking-gap rule to
`qa_loop/rules.py::evaluate_qa_gaps` (lead text must flow through the existing
`_scrub_gap`/`_scrub_text` path — do not hand-format an unscrubbed `reason`;
deferrable via `acknowledged_gaps`). Wire `qa_audit_summary()` into
`qa_loop/tool.py::_build_review_context` (extend `signal_text`, carry
`_audit_leads` — **ids/enums only**), mirroring the `qa_loot_summary` wiring.

### Task 2.4 — Prompt lines (two files, mind the anchor)
Add the **root line** (thesis auto-refreshes after each specialist; read
`get_audit_state`, act on high-priority leads, mark leads `done`/`dropped`) to
`strix/skills/coordination/root_agent.md` (NOT `system_prompt.jinja` — it has no
`is_root` conditional). Add the **shared** read-`get_audit_state`-before-new-
surface line to `system_prompt.jinja`. No new tool; `select_tools` unchanged; no
spawn convention.

**Acceptance:** see [03-strategist-loop.md](03-strategist-loop.md#acceptance-criteria).

---

## Test plan

Prefer pure/mocked tests. Use `XXXX` for all placeholders.

### `tests/test_audit_state.py`

1. `test_update_creates_thesis_assumption_lead` — one `update_audit_state`
   setting thesis + an assumption + a lead populates the doc; ids returned.
2. `test_update_rejects_unknown_enums` — bad `confidence`/`priority`/
   `lead_status` → `success False` with the valid set listed.
3. `test_update_requires_confidence_with_assumption_and_priority_with_lead` —
   missing companion field → clean error.
4. `test_supersede_marks_old_and_links` — superseding assumption `X` sets `X`
   `status="superseded"` + `superseded_by`, new one `supersedes=X` + `reason`.
5. `test_supersede_unknown_or_already_superseded_errors` — both → clean error,
   no mutation.
6. `test_get_audit_state_hides_superseded_by_default` — default view omits
   superseded; `include_superseded=True` shows them.
7. `test_update_lead_status_transitions` — `open→in_progress→done`; unknown
   `lead_id` → clean error.
8. `test_strings_and_collections_bounded` — oversized thesis/text/refs truncated;
   active assumptions/leads capped; superseded-history cap evicts oldest.
9. `test_persist_and_hydrate_roundtrip` — `_persist` then
   `hydrate_audit_state_from_disk` restores the doc.
10. `test_hydrate_handles_malformed_json` — garbage file → empty doc, no raise.
11. `test_qa_audit_summary_refs_are_ids_only` — refs carry only ids/enums
    (`lead_id`/`priority`/`status`, `assumption_id`/`confidence`); **no `text`
    field** in refs; free text appears only in `signals` (lowercased, scrubbed);
    no raw values anywhere.
11a. `test_update_audit_state_scrubs_free_text` — a `thesis`/`lead`/`reason`
    containing a scrub-matched token (e.g. a `Bearer XXXX`-shaped string) is
    stored redacted (write-time `scrub_secrets` backstop).
12. `test_get_audit_state_empty_store_is_safe` — empty thesis + empty lists, no
    error.
13. `test_audit_state_stores_ids_not_values` — a lead/assumption written with
    `refs=["ab12cd"]` keeps the id; `qa_audit_summary` exposes no value field
    (guards the ids-only secret convention).

### `tests/test_reflection_loop.py`

All offline. The model is **mocked** (monkeypatch `litellm.acompletion` /
`reflection`'s call site) — no live LLM/Docker/Caido.

14. `test_build_reflection_input_is_pure` — given a snapshot (prior thesis, loot
    refs by id, target profile, current QA gaps), returns chat messages that
    include the prior thesis and lead ids and **no raw values**; deterministic,
    no I/O.
15. `test_apply_reflection_applies_delta` — a well-formed structured result
    (thesis + a supersede + a new lead + a lead status update) is applied to
    `audit_state` correctly (supersede links both ways; lead added; status
    changed).
16. `test_apply_reflection_ignores_malformed_items` — a result with a bad enum /
    missing field / unknown supersede id applies the valid items and drops the
    bad ones **without raising**.
17. `test_run_reflection_tolerates_bad_json_then_skips` — mocked model returns
    non-JSON twice → `run_reflection` retries once, then skips (audit_state
    unchanged), returns cleanly, does not raise.
18. `test_run_reflection_skips_when_budget_stopped` — `coordinator.budget_stopped`
    True → no model call, no change.
19. `test_run_reflection_records_usage` — on a successful mocked call, usage from
    the litellm response is recorded to `report_state` (budget accounting).
20. `test_on_agent_end_triggers_for_child_not_root` — a child end
    (`parent_id` set) schedules a reflection; a root end (`parent_id is None`)
    does not (assert via a monkeypatched `run_reflection` counter; fake context).
21. `test_reflection_single_flight_coalesces` — two near-simultaneous child ends
    while a reflection is "running" result in at most one extra run (drive the
    lock/`dirty` logic with a controllable fake).
22. `test_qa_gap_for_open_high_lead` — seed an open `priority=high` lead,
    `evaluate_qa_gaps` → a finish-blocking gap naming the lead.
23. `test_no_qa_gap_when_lead_done_dropped_or_lower` — `done`/`dropped`/`medium`/
    `low` → no blocking gap.
24. `test_lead_gap_can_be_acknowledged` — acknowledging the lead-gap's `gap_id`
    (via `assemble_review(acknowledged_gaps=[...])`) removes it from the blocking
    set (livelock escape hatch).
25. `test_lead_gap_reason_is_scrubbed` — an open high lead whose `text` contains a
    scrub-matched token yields a gap whose persisted `reason`/`suggested_action`
    is redacted (guards the `_scrub_gap` routing).
26. `test_qa_loop_surfaces_audit_signals` — seed the store (call the `_impl`
    after `hydrate_audit_state_from_disk(tmp_path)`), monkeypatch
    `qa_loop.tool._collect_proxy` → `([], False)`, call `_build_review_context`
    **synchronously (sync def — no `await`)**, assert an audit signal in
    `signal_text` and `_audit_leads` carried (ids/enums only). Mirrors
    `tests/test_loot_store.py::test_qa_loop_surfaces_loot_signals`.
27. `test_base_tools_include_audit_state_tools` — `get_audit_state`,
    `update_audit_state` present in `_BASE_TOOLS` by tool name.
28. `test_select_tools_adds_no_new_root_tool` — `select_tools(is_root=True)`
    contains exactly the pre-existing root extras (`review_before_finish`,
    `finish_scan`) plus base — guards against accidentally adding a bespoke tool.
29. `test_root_agent_skill_mentions_audit_state` — `root_agent.md` mentions
    `get_audit_state` / the auto-refresh convention (mirrors
    `test_finish_scan_guards.py::test_root_agent_skill_mentions_review_before_finish`).

---

## Manual verification (not part of automated DoD)

```bash
uv run pytest tests/test_audit_state.py tests/test_reflection_loop.py
uv run pytest
make lint
make type-check
```

Smoke test (authorised target only; identifiers as `XXXX`):

```bash
uv run strix -n --target https://XXXX.example --scan-mode deep --max-budget-usd 1
```

Expected: after a child specialist finishes, a reflection runs automatically
(no manual spawn) and `audit_state.json` appears in the state dir with a thesis +
leads; superseded assumptions carry a reason; the finish gate blocks while an
open high-priority lead remains (until pursued or acknowledged); reflection
tokens show up in the usage/budget totals; `audit_state.json` and the thesis
survive a resume. Confirm no raw secret values appear in `audit_state.json` (ids
only).
