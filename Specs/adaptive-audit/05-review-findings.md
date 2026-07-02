# Spec Review — Adaptive Audit (05-review-findings)

Two independent read-only reviews of `Specs/adaptive-audit/{README,01..04}.md`,
each verified against the current tree: an **architecture/alignment** pass
(anchors, internal consistency, overengineering, testability, trigger soundness)
and a **security** pass (secret discipline of the derived-intel store). No
implementation.

## 0.1 Functional review (round 2) — vehicle change to a code path

A second, deeper pass traced the *runtime mechanism* (not just anchors) and found
that the round-1 design's vehicle — a **strategist child agent** — had multiple
functional failure modes, all rooted in "the strategist is a full child agent."
Verified against the tree, then fixed by switching Feature 2 to a **dedicated
reflection code path** triggered by `on_agent_end` and calling the run's model
directly via `litellm`. What changed and why:

| # | Sev | Problem (round-1 design) | Resolution (code-path design) |
|---|-----|--------------------------|-------------------------------|
| F1 | High | Self-trigger loop: "reflect after any child ends" + the strategist *is* a child → it triggers itself. | Reflection is not an agent → cannot fire `on_agent_end`; the trigger also filters to child ends only. Bug cannot occur. |
| F2 | High | `create_agent` enforces `_MAX_ACTIVE_CHILDREN_PER_PARENT`; a busy root can't spawn the strategist exactly when reflection matters. | No spawn — a code path has no child-cap interaction. |
| F3 | High | Prompt-convention trigger is the weak link on local models; the "deterministic trigger = execution-loop surgery" claim was wrong. | `RunHooks.on_agent_end` verified to exist and the hooks object is already wired to every agent → deterministic trigger is cheap and is now v1. |
| F4 | Med | "non-blocking integration" contradicted "spawn strategist and WAIT". | Reflection scheduled as a non-blocking task; contradiction gone. |
| F5 | Med | `create_agent(inherit_context=True)` would inherit the root's bloated context, defeating "focused context". | Snapshot assembled in code — focused by construction, nothing inherited. |
| F6 | Med | A child strategist holds Shell + `create_agent` + finish/report → "analysis only" is prompt-only; can violate single authority. | The code path has **no tools** → single authority enforced structurally. |
| F7 | Med | Finish-gate livelock if leads are minted faster than closed. | Lead-gaps reuse the existing `acknowledged_gaps` path ("defer" = ack); + cap on open-high leads + sparing-`high` prompt guidance. |
| F8 | Low | Strategist ignored the existing `evaluate_qa_gaps` output. | The snapshot now includes current QA gaps as an input. |
| N1 | Med | Local models vary in structured-output support. | Tolerant parse + one retry + clean skip; never crashes. |
| N2 | Med | A direct model call escapes the SDK usage hook → invisible to `--max-budget`. | Reflection records its litellm usage into `report_state`; skips when `budget_stopped`. |
| N3 | Low | Childless / non-deep runs. | Documented scope limits: child-completion-keyed trigger; deep-only finish backstop. |

Net: the code-path vehicle removed four failure modes by construction, made the
trigger deterministic, and made the whole loop **unit-testable** (the strategist
agent would have been manual-smoke-only). It is more code than reusing
`create_agent`, but that reuse was not actually functional. Feasibility verified:
`litellm` is already a dependency, globally configured in
`strix/config/models.py`, model id from `STRIX_LLM`; `on_agent_end` exists on
`RunHooks`.

## 0.2 Integration / regression review — will it break the current audit?

A pass focused on how the design touches the **live** run loop, to catch
regressions to the existing audit. Verified against the tree; all fixed in-spec.

| # | Sev | Regression / misalignment | Fix applied |
|---|-----|---------------------------|-------------|
| R1 | High | `review_before_finish → _run_review → _build_review_context` runs on **every deep-scan finish** and is **not** wrapped in try/except (confirmed). A throw in the new `qa_audit_summary()` or lead-gap rule would break the finish gate for *all* deep scans, feature-used or not. | Hard requirement + test: `qa_audit_summary` and the lead rule must be null-safe / never raise on empty/missing `audit_state`; regression test runs the full path on an **empty** store (01 §4, 03 acceptance, 04 test 22). |
| R2 | High | The run resolves models via `StrixProvider` (`ollama/X → ollama_chat/X`); the spec's raw `litellm.acompletion(model=STRIX_LLM)` would **mis-route ollama** (the likely local setup) → reflection silently always fails → feature dead. | **Dissolved (see §0.3):** mirror `report/dedupe.py` — `StrixProvider().get_model(STRIX_LLM).get_response(...)` reuses the run's routing; no manual mapping, no raw litellm. |
| R3 | Med | Reflection raw-iterating the store module-dicts while agents write → `RuntimeError: dict changed size during iteration`. | Read via the lock-protected accessors / snapshot under each store lock (01 §3, 03; test 19a). |
| R4 | Med | `record_sdk_usage` needs an SDK `Usage` object a raw litellm response lacks → budget accounting would break. | **Dissolved (see §0.3):** `get_response` returns an SDK response with `.usage`, so `record_sdk_usage(usage=resp.usage)` is correct (as `report/dedupe.py` does). 01 §3, 03; test 19. |
| R5 | Med | Hooks propagate exceptions by design (`on_llm_end` raises `BudgetExceededError`); a raising `on_agent_end` runs on the agent-teardown path and could disrupt an agent. | Hook body tiny + fully wrapped (never raises); reflection runs as an exception-isolated task; skip on `budget_stopped`/`is_shutting_down` (01 §3, 03; test 20a). |
| R6 | Low | Reflection applying via the pure helpers without the store lock races the root's `update_audit_state`. | Apply under `_audit_state_lock` (03; test 19a). |

Non-issues (verified safe): non-deep scans never execute the QA wiring (no
regression there); empty `audit_state` is safe once R1 holds; hydration is
tolerant like the other stores; `select_tools` is unchanged so existing tool flow
is untouched; the +2 base tools add prompt-token cost (cumulative with Tool
Awareness's +6) but break nothing — noted, not blocking.

## 0.3 Final deep pass — anchors verified, R2/R4 dissolved

Every Design-B anchor was executed/read against the tree. All present and behave
as the spec assumes:

- **`RunHooks.on_agent_end` / `on_tool_end` exist** (`uv run python` introspection)
  and `ReportUsageHooks` is passed to every agent + the root run.
- **Hook context is reachable:** the inner context dict carries `agent_id`,
  `parent_id`, `coordinator`, `caido_client` (`runner.py:242–251`); children set
  `parent_id` (`execution.py:615–616`); hooks read `context.context` exactly as
  `on_llm_end` does. So the child-vs-root filter and `caido_client` access are
  real.
- **`coordinator.budget_stopped`** (property) and **`is_shutting_down`** (attr,
  `agents.py:55/71`) both exist for the skip guards.
- **Standalone model call has proven prior art:** `report/dedupe.py:189–214` and
  `interface/main.py:257` already do `StrixProvider().get_model(name)
  .get_response(...)` outside the Runner, record `resp.usage` via
  `record_sdk_usage`, and tolerantly parse prompt-instructed JSON — an exact
  template for the reflection. **Adopting it dissolves R2 and R4:** `get_model`
  reuses the run's routing (no manual `ollama_chat` mapping), and `resp.usage`
  makes `record_sdk_usage` correct (no `completion_cost`/`observed_cost` needed).
  The spec (01 §3, 03) now mandates mirroring dedupe.
- **`record_sdk_usage` → budget:** feeds `_total_cost` → `get_total_llm_cost()` →
  the `on_llm_end` `BudgetExceededError` check (`report/usage.py`, `hooks.py`).
- **`_run_review` unguarded** (`qa_loop/tool.py`) — R1's regression guard stands;
  the empty-store full-path test (04 test 22) covers it.

**Conclusion: no remaining problems, regressions, or audit-breakers.** The design
touches two shared hot paths (the QA finish gate and the lifecycle hooks); both
are covered by explicit, tested guards (R1 null-safe QA; R5 non-raising hook).
The model call now reuses an existing in-tree pattern rather than new plumbing.
**Verdict: GO — ready to build.**

## 0. Resolution status (round-1 fixes applied to the spec)

All round-1 findings below were applied. (Some — the `system_prompt.jinja`
root-only anchor, the `review_findings` removal — are now moot because Feature 2
no longer spawns an agent at all; they remain logged for history.) The verdict
after both rounds is **GO** — a smaller agent can execute the spec as written.
Change log:

| # | Sev | Fixed in | What changed |
|---|-----|----------|--------------|
| 1 | Critical | `01` §5, `03` trigger/acceptance, `04` Task 2.3 | Root-only convention retargeted from a non-existent "root-only region of `system_prompt.jinja`" to `strix/skills/coordination/root_agent.md` (auto-appended for root by `prompt.py`). Shared line stays in the jinja. |
| 2 | High | `01` diagram, propagation, spawn-anchor, testing strategy | Purged all remaining v1 `review_findings` references (contradicted the "no new tool in v1" redesign). It stays only as a fenced phase-2 option. |
| 3 | High | `01` testing strategy, `04` test 18 | Named the QA-wiring template test by full path (`tests/test_loot_store.py::test_qa_loop_surfaces_loot_signals`) and flagged `_build_review_context` is **sync** (no `await`) + seed via `_impl`. |
| 4 | Medium (sec) | `01` §4 + §Secret discipline, `02` `qa_audit_summary` + acceptance, `04` test 11 | `qa_audit_summary` `refs` are **ids/enums only** (no free text) — refs are persisted into the QA review; free text goes only to in-memory `signals`. Matches `qa_loot_summary`. |
| 5 | Medium (sec) | `01` §4, `03` QA-gate rule + acceptance, `04` Task 2.2 + test 19 | The new lead-gap rule (first QA rule with a dynamic field) must route `lead.text` through the existing `_scrub_gap`/`_scrub_text` path before persistence; added as an explicit, testable criterion. |
| 6 | Low→applied (sec) | `01` + `02` §Secret discipline + behaviour, `04` test 11a | `update_audit_state` runs `scrub_secrets` over free-text fields (`thesis`/`assumption`/`lead`/`reason`) at write time — a cheap defense-in-depth backstop for the ids-only convention (no write-rejection). |
| 7 | Medium | `01` trigger, `03` QA-gate rule | Stated the backstop is **deep-scan-only** (`qa_loop_enabled == (scan_mode=="deep")`); dropped the unqualified "cannot complete un-reconciled" claim. |
| 8 | Medium | `02` `update_audit_state` note | Added the SDK strict-schema `T | None = None` requirement and called out the `rationale`-via-`reason` param overload (write an assumption **or** a lead per call when `reason` is set). |
| 9 | Medium | `03` acceptance, `04` test 22 | Added a test for the root convention (mirrors `test_root_agent_skill_mentions_review_before_finish`) — every other prompt convention in the tree has one. |
| 10 | Medium | this file | The referenced `05-review-findings.md` did not exist; this document resolves that reference. |
| 11 | Low | `04` §v1 scope | Added an explicit "build exactly these files" manifest to fence scope against a smaller agent. |
| 12 | Low | `01` §2 hydration | Noted the runner hydration imports are **function-local** (inside `run_strix_scan`, hence the `PLC0415` ignore) — add the new import there, not at module top. |

## 1. Verdicts (as first written)

- **Architecture:** GO-WITH-FIXES. Sound, lean, almost entirely well-anchored;
  one Critical wrong anchor + dangling `review_findings` references were the only
  blockers. Overengineering assessment: **pass** — the 2-tool surface, the
  no-new-tool Feature 2, and the folding of assumption-revision into the store
  are the lean choices; phase-2 material is correctly fenced.
- **Security:** No Critical/High. Two Mediums (persisted-`refs` free text; the
  gate rule's dynamic text) + one Low (convention-only enforcement), all narrow
  single-file corrections that pull the spec back in line with the established
  `loot`/`notes` pattern. `0644` (not `0o600`) confirmed correct: the raw secret
  still lives only in `loot.json` (`0o600`); `audit_state` holds a pointer-by-id
  map at the same trust level as `notes`/`target_profile`.

## 2. Verified CORRECT (coverage, from both passes)

- Store template (`notes/tools.py`: dict + `RLock` + atomic `_persist` + tolerant
  hydrate) — present; the single-document variant reuses it fine (`target_profile`
  already proves a non-notes shape on the same lifecycle).
- `_BASE_TOOLS` + `select_tools` (`factory.py:333-373`): children get
  `[*_BASE_TOOLS, agent_finish]`; adding 2 base tools reaches every agent incl.
  the strategist child, with **no** `select_tools` change. Correct.
- Child reaches the blackboard: `get_loot`/`get_target_profile`/`traffic_health`/
  `list_notes` are all in `_BASE_TOOLS`. Correct.
- *(Round-1 only; superseded by §0.1 — Design B spawns no strategist agent, so
  `create_agent`/`skills` are not used by this feature. `send_message_to_agent`/
  `wait_for_message` remain the root's existing interrupt/collect tools.)*
- QA wiring (`_build_review_context` + `evaluate_qa_gaps`) extends cleanly, and
  the new blocking gap flows through `_qa_review_blocker` → blocks `finish_scan`.
  Backstop mechanism verified end-to-end (deep scans).
- `load_skills` resolves by filename stem; `reconnaissance/` exists.
- `scrub_secrets`, `mask_value`, and the `_scrub_gap` gap-scrubber all exist as
  assumed.
- Secret model consistent with `loot`/`target_profile`: derived-intel, ids-only,
  notes-style `0644`, same bundle/transcript caveat; no new exfil *path* (only
  higher write-frequency, mitigated by the write-time scrub).

## 3. Deferred (phase-2, explicitly NOT in v1)

- A thin `review_findings` convenience tool (only if freehand orchestration
  proves unreliable on the target local model).
- A deterministic execution-loop trigger (`core/execution.py` / coordinator
  seam) — more consistent, but touches the loop.
- Multi-sample self-consistency or a critic pass — add only if a single
  strategist pass measures inconsistent.
- Non-deep scans have no deterministic backstop (prompt-convention only) — by
  design; revisit only if adaptive steering is wanted on quick/standard scans.
