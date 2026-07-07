# 01 — Architecture

> Extends [README.md](README.md). All file paths are relative to the repo root.
> Grounded in the code as of branch `feat/llm-concurrency-limit`.

## Component map

| Concern | File | Change |
|---|---|---|
| Token estimate + budget + margin | `strix/core/context_limit.py` | Layer 0: honest meter, percentage reserve, count instructions |
| Config knobs | `strix/config/settings.py` | Layer 0/1: default window < model max; compaction ratio + recency-window knobs |
| In-place session mutation | `strix/core/sessions.py` | Layer 1: `compact_session`; Layer 2: `rehydrate_working_context` |
| Structured summary call | `strix/core/reflection.py` (pattern reuse) | Layer 1: one bounded model call to summarise the old span |
| Blackboard readers | `strix/tools/{audit_state,notes,loot,reporting,todo,agents_graph}` | Layer 2: reuse existing `*_summary` / `list_*` / `_snapshot_*` readers |
| Retry / recovery loop | `strix/core/execution.py` | Layer 4: pre-flight fit, progressive back-off, compaction-only turn |
| Parent notification size | `strix/tools/agents_graph/tools.py` | Layer 3: cap ingested `result_summary` |
| Successor handoff | `strix/tools/agents_graph/tools.py` + `sessions.py` | Layer 5: `spawn_successor` seeded from `rehydrate` |

The `ContextLimitFilter` is wired once into the shared `RunConfig` as
`call_model_input_filter` ([`runner.py:197`](../../strix/core/runner.py)); one
instance covers root and all children. Sessions are `SQLiteSession`s opened per
agent in `{state_dir}/.state/agents.db`
([`sessions.py:19`](../../strix/core/sessions.py)).

## Session item model (what compaction operates on)

`await session.get_items()` returns an ordered list of dict items. Relevant
types (OpenAI Responses shape used by the SDK):

- `{"role": "user"|"assistant", "content": ...}` — messages.
- `{"type": "function_call", "call_id", "name", "arguments"}` — a tool call.
- `{"type": "function_call_output", "call_id", "output"}` — its result.

**Pairing invariant:** a `function_call` and its `function_call_output` share a
`call_id` and must never be separated — replaying a call without its output (or
vice-versa) is what produces the malformed-history `400`s that
`repair_malformed_tool_calls_in_session` already guards against. Compaction must
treat a call+output as one atomic unit and must not leave an orphan at the
recency-window boundary. Reuse the orphan-dropping helper already in
`context_limit.py` (`_drop_orphaned_*` used by `trim_items`).

Mutation pattern (established by the two existing mutators):

```python
items = await session.get_items()
rebuilt = transform(items)
await session.clear_session()
await session.add_items(rebuilt)   # cast to list[TResponseInputItem]
```

---

## Layer 0 — Honest meter + margin (prerequisite; also fixes the live 400)

**Problem:** budget target = model limit − 16 384, computed from a bytes/4
estimate that ignores `instructions` and under-counts token-dense content →
"trimmed" requests still overflow, and the back-off can't recover.

**Changes in `context_limit.py`:**

1. **Count instructions.** `ContextLimitFilter.__call__` receives
   `model_data.instructions` (the system prompt) but never counts them. Include
   `_est_tokens_text(model_data.instructions)` in the budget arithmetic so the
   fixed system-prompt overhead is reserved, not ignored.
2. **Percentage reserve, not a flat constant.** Replace the flat
   `_RESERVE_TOKENS = 16384` floor with `max(_RESERVE_TOKENS, ceil(window *
   _RESERVE_RATIO))`, `_RESERVE_RATIO` default `0.10`. On a 262 144 window this
   reserves ~26 k, absorbing estimate drift + output tokens.
3. **Calibrated estimate.** Keep the dependency-free bytes/N heuristic but make
   `_APPROX_BYTES_PER_TOKEN` configurable and default it **conservative** (3.5,
   i.e. assume *more* tokens per byte) so the estimate errs toward over-counting
   for code/JSON/base64. Document that a real tokenizer is a future swap only if
   this proves too coarse (it need not be exact — it needs a safe margin).
4. **Default window below the model max.** Change `settings.py` default
   `context_window` from `262144` to a headroom'd value, or — better — keep the
   configured number as the *model* limit and always compute the budget as
   `min(configured, learned) * (1 - _RESERVE_RATIO) - instruction_tokens`. Either
   way the outbound request targets well under the hard limit.

**Acceptance:** given a synthetic history whose *real* token count sits between
`window*(1-ratio)` and `window`, `ContextLimitFilter.__call__` trims it to fit
with the instruction overhead included; a request that previously produced the
double-`400` now fits on the first retry.

---

## Layer 4 — Graceful back-off (build second; stops agents dying today)

**Changes in `execution.py` `_run_cycle` (the `except Exception` recovery
block, ~lines 402–439):**

1. **Progressive budget shrink.** Add a `ContextLimitFilter.shrink(factor:
   float) -> bool` that multiplies the *effective budget* (not the learned
   window) by `factor` (e.g. 0.85) and returns `True` while a floor is not hit.
   In the retry branch, when `input_rejected` and
   `note_context_length(reported_max)` returns `False` (reported max unchanged —
   the current dead end), fall back to `context_filter.shrink(0.85)` and retry.
   This is the fix for the parked-agent failure: the budget keeps shrinking even
   when the provider keeps reporting the same max.
2. **Pre-flight fit + compaction.** Before shrinking, attempt
   `await compact_session(session, ...)` (Layer 1). If it changed the session,
   retry immediately (a compaction counts against a small bounded counter, like
   `context_relimits < 3`).
3. **Compaction-only last resort.** If shrinking has hit its floor and
   compaction can't reduce further, emit one summarised "continue from here"
   turn via `rehydrate_working_context` (Layer 2) rather than parking. Only park
   as failed if *that* also rejects (genuinely unrecoverable).
4. Keep the existing image-strip and tool-call-repair recovery branches; add
   compaction ahead of them in the recovery order.

**Acceptance:** a unit test that forces two consecutive context-length `400`s
with an unchanged reported max asserts the agent recovers (shrinks/compacts and
succeeds) instead of `status = failed`.

---

## Layer 1 — Rolling in-place compaction

**New function in `sessions.py`:**

```python
async def compact_session(
    session: Session,
    *,
    keep_recent: int,           # number of trailing items kept verbatim
    summarize: SummarizeFn,     # async (old_items, blackboard_index) -> str
    blackboard_index: str,      # pre-rendered pointer index (Layer 2)
    est_tokens: Callable,       # honest meter from Layer 0
    target_tokens: int,         # compact until <= this
) -> bool:                      # True if the stored session was rewritten
```

**Algorithm:**

1. `items = await session.get_items()`. If `est_tokens(items) <= target_tokens`,
   return `False` (nothing to do).
2. **Partition:** `pinned = items[0]` (the agent's task/objective — always kept);
   `recent = last keep_recent items, extended left to not split a call/output
   pair`; `old = the middle span`.
3. If `old` is empty (task + recent already exceed target), return `False` — this
   is the signal Layer 4/5 uses to escalate to shrink or successor handoff.
4. **Summarise `old`** via one bounded structured model call (same mechanism as
   `reflection.py`), *seeded with `blackboard_index`* so the summary is grounded
   in persisted state, not free recall. The summary is written as a **pointer
   index**: what was decided, what was tried-and-clean (negative results), and
   IDs into the stores (`note#`, `loot#`, `vuln-#`, lead IDs) — not verbatim
   payloads.
5. **Rebuild:** `[pinned, {"role": "user", "content": _COMPACTION_MARKER +
   summary}, *recent]`; drop any orphaned call/output; `clear_session()` +
   `add_items(rebuilt)`.
6. Return `True`.

**Trigger (proactive):** in `ContextLimitFilter.__call__`, when
`est_tokens(...) >= window * _COMPACTION_TRIGGER_RATIO` (default `0.70`), request
compaction. Because the filter is a synchronous input filter and compaction is
async + mutates the session, do **not** compact inside `__call__`; instead set a
flag / enqueue via the coordinator so the *next* `_run_cycle` turn compacts
before calling the model (an `on_turn_start`/pre-call hook in `execution.py`).
The synchronous filter remains the hard safety net (trim outbound); compaction
is the proactive, persistent reducer.

**Config knobs (`settings.py`, `llm` section, `STRIX_LLM_*`):**
`compaction_trigger_ratio` (0.70), `compaction_keep_recent` (e.g. 12 items),
`reserve_ratio` (0.10), `bytes_per_token` (3.5).

**Why in-place + persisted:** rewriting the stored session is what bounds growth
and fixes resume — the mutation precedent (`strip_all_images_from_session`) shows
`clear_session` + `add_items` is the supported, safe way to do it.

---

## Layer 2 — Blackboard rehydrate (index, deterministic)

**New function** (in `sessions.py` or `reflection.py`), pure reads, no model
call:

```python
def render_blackboard_index(state_dir: Path) -> str
```

Assembles a compact, bounded **pointer index** from the existing readers — no
new storage:

- `audit_state`: `_snapshot_audit_state()` → working thesis, assumptions
  (with confidence), prioritised **leads** (IDs + status).
- `notes`: `list_notes(metadata_only=True)` / `qa_notes_summary()` → note IDs +
  titles + tags (bodies fetched on demand via `get_note`).
- `loot`: `qa_loot_summary()` → loot IDs + masked labels.
- `reporting`: filed vuln IDs + titles (the CSV/report index).
- `todo`: open todos.
- `agents_graph`: `view_agent_graph` digest (who exists, status).

Two consumers:

1. **Compaction seed** (Layer 1 step 4) — grounds the summary.
2. **Index auto-injection** — the compaction marker embeds this index so the
   model always *sees* what is available and fetches full records by ID
   (`get_note`, `get_loot`), instead of relying on the model remembering to look.
   This closes the "model-driven retrieval" gap: the index is always in front of
   it.

**Bounded:** the readers already cap output (`_MAX_QA_NOTES`, lead/assumption
bounds, tag bounds), so the index has a hard size ceiling regardless of run
length. This is why no vector DB is needed.

---

## Layer 3 — Thin root

The root already coordinates and does not test directly
([`skills/coordination/root_agent.md`](../../strix/skills/coordination/root_agent.md)),
and children hand off a narrative `result_summary` + `findings` list via
`agent_finish` ([`agents_graph/tools.py:547`](../../strix/tools/agents_graph/tools.py)) —
not their transcript. The remaining growth is (a) the root's own coordination
turns and (b) unbounded `result_summary` text ingested per child.

**Change:** cap the `result_summary` length appended to the parent's context to a
bounded digest (first N chars / a truncation marker), with the full summary
preserved in a `note` (ID referenced in the digest). Full evidence already lives
in `loot`/vuln reports. This keeps the coordinator small far longer *without any
summarisation cost*, and is the single biggest lever for the run that failed
(12 children × verbose summaries + 3 h of coordination turns).

---

## Layer 5 — Successor handoff (escape hatch only)

Trigger: Layer 1 step 3 returned `False` (task + recency window alone exceed
target) **and** Layer 4 shrink hit its floor — a single agent genuinely too big
to fit. Then:

- `spawn_successor(agent_id)`: create a new agent of the same role/parent,
  seed its session with `[task, render_blackboard_index(...), last few items]`,
  re-point `agents_graph` parent/child edges and any pending
  `send_message_to_agent` routing to the successor, transfer budget accounting,
  mark the predecessor `superseded` (not `failed`).
- Seeded from **structured `rehydrate`, not prose** — this is the correct,
  bounded form of the "orchestrator_N" idea, generalised to any agent and freed
  of prose drift and graph surgery (edges are explicitly re-pointed).

Kept minimal and last because Layers 0–4 make it rare: an agent should almost
always compact rather than hand off.

## Non-goals

- No vector database, embeddings, or semantic search. Deterministic
  index + get-by-ID over bounded stores is sufficient and simpler.
- No new persistent store — Layer 2 reads the stores that already exist.
- No exact tokenizer requirement — a conservative estimate + percentage margin
  is enough; a real tokenizer is a later drop-in behind `est_tokens` if needed.
- No change to child→parent handoff *semantics* (still `result_summary` +
  `findings`); Layer 3 only bounds the ingested size.
