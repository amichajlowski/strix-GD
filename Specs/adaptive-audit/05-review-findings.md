# Spec Review — Adaptive Audit (05-review-findings)

Two independent read-only reviews of `Specs/adaptive-audit/{README,01..04}.md`,
each verified against the current tree: an **architecture/alignment** pass
(anchors, internal consistency, overengineering, testability, trigger soundness)
and a **security** pass (secret discipline of the derived-intel store). No
implementation.

## 0. Resolution status (fixes applied to the spec)

All blocking and should-fix findings below have been applied to the spec docs.
The verdict is now **GO** — a smaller agent can execute the spec as written.
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
- `create_agent(name, task, inherit_context, skills)` accepts
  `skills=["audit_strategy"]`; `validate_requested_skills` passes once the skill
  file exists. `send_message_to_agent`/`wait_for_message` signatures match.
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
