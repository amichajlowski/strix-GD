# 03 — Developer-Agent Handover Prompt

Copy/paste the block below to the implementing developer agent. It is
self-contained; it points at the spec files for detail rather than repeating them.

---

You are implementing the **Context Compaction** feature in the Strix codebase
(`/Users/amichajlowski/Tools/AI/strix`, Python 3.12, branch
`feat/llm-concurrency-limit` or a new branch off it). Read these first, in order:

- `Specs/context-compaction/README.md` — goal, the production bug it fixes, and
  the rejected alternative.
- `Specs/context-compaction/01-architecture.md` — the design and exact files.
- `Specs/context-compaction/02-task-list-and-tests.md` — ordered tasks T1–T7 with
  their tests. **This is your work list. Follow the build order.**

## The bug you are fixing (ground truth)

A resumed run (`strix_runs/host-docker-internal-3000_6ed5/strix.log`, ~11:14:01)
died with `openai.BadRequestError: 400 — maximum context length is 262144
tokens ... your prompt contains at least 262145 input tokens`. The SDK stores
each agent's full transcript and replays it every turn; `context_limit.py` trims
only the outbound copy and never mutates the stored session, so history grows
unbounded and, on the second identical-max `400`, `execution.py` parks the agent
as failed. Read the log lines around `11:14:01–02` and
`strix/core/context_limit.py` (esp. `note_context_length`, `_budget`,
`_est_tokens`) and `strix/core/execution.py` (`_run_cycle` recovery block ~402–439)
before writing code.

## Non-negotiable constraints

1. **Reuse, don't rebuild.** `compact_session` goes in `strix/core/sessions.py`
   beside `strip_all_images_from_session` and
   `repair_malformed_tool_calls_in_session` and uses the *same*
   `get_items() → clear_session() → add_items()` mutation pattern. The
   blackboard index reads the **existing** stores (`audit_state`, `notes`,
   `loot`, `reporting`, `todo`, `agents_graph`) via their existing summary/list
   readers — **do not add a new store, database, vector index, or embedding
   dependency.**
2. **Preserve the tool-call pairing invariant.** A `function_call` and its
   `function_call_output` (same `call_id`) must never be split by compaction or
   the recency boundary. Reuse the orphan-dropping helper already used by
   `trim_items` in `context_limit.py`. A split pair reintroduces the malformed-
   history `400` that `repair_malformed_tool_calls_in_session` exists to prevent.
3. **Compaction mutates the persisted session** — that is the point (bounds
   growth, fixes resume). But it must be safe: mirror the clear+add error handling
   in the existing mutators.
4. **The synchronous input filter stays the hard safety net.** Compaction is
   async and session-mutating, so it must run in the turn loop
   (`execution.py`), not inside `ContextLimitFilter.__call__`. Do not make the
   filter async.
5. **Never park an agent on a recoverable size `400`.** The recovery order is:
   compact → shrink budget → one blackboard "continue" turn → (only then) park.
6. **TDD.** Write each task's tests first and watch them fail, then implement.
   The single most important test is `test_double_context_400_recovers`
   (T2) — it reproduces the exact observed failure and must go from RED to GREEN.
7. **Immutability / style.** Follow the repo conventions: type annotations on all
   signatures, small focused functions, no in-place mutation of Python data
   structures (build new lists), `ruff`/`mypy` clean.

## Build order (land in slices)

- **Slice 1 (ship alone — fixes production):** T1 (honest meter + margin) + T2
  (progressive back-off). After this, the `_6ed5` failure recovers. Open a PR.
- **Slice 2:** T3 (`render_blackboard_index`) + T4 (`compact_session`) + T5
  (wire into turn loop and recovery).
- **Slice 3:** T6 (thin-root notification cap).
- **Slice 4 (optional):** T7 (`spawn_successor` fallback). Only if Slices 1–2
  leave a real case where a single agent cannot be compacted to fit.

## Config knobs to add (`strix/config/settings.py`, `llm` section, `STRIX_LLM_*`)

`reserve_ratio` (0.10), `bytes_per_token` (3.5), `compaction_trigger_ratio`
(0.70), `compaction_keep_recent` (12). Keep the existing `context_window` /
`STRIX_LLM_CONTEXT_WINDOW` but always derive the outbound budget as
`min(configured, learned) − reserve − instruction_tokens`, floored at
`window // 2`. Document the new knobs in the config sample the way
`STRIX_LLM_MAX_CONCURRENCY` is documented (see recent commit history).

## Verification before you hand back

- `make check` (ruff + mypy) clean; `pytest` green; ≥ 80% coverage on new code.
- The regression test `test_double_context_400_recovers` passes.
- An integration test that loops many turns shows the stored session size
  **plateaus** instead of growing without bound, and a resume of the compacted
  session does not re-overflow.
- Confirm no new dependency entered `pyproject.toml` / `uv.lock`.

## What NOT to do

- Do not implement the "spawn `orchestrator_1/2/3` from a prose summary" chain as
  the primary mechanism — it is root-only, lossy, and breaks graph routing (see
  README "Why not"). Successor handoff (T7) is a bounded, index-seeded escape
  hatch only.
- Do not add semantic search / embeddings / a vector DB.
- Do not change the child→parent `agent_finish` semantics (still
  `result_summary` + `findings`); T6 only bounds the ingested prose size.
- Do not make `ContextLimitFilter.__call__` async or move state persistence out
  of the SDK session.

Report back with: the slice-1 diff + passing regression test first, then proceed
through the slices, pausing after each for review.
