# Context Compaction — Bounded, Blackboard-Anchored Agent Memory

## Goal

Make every Strix agent (root orchestrator **and** child specialists) run
indefinitely without ever exceeding the model's context window, by **compacting
its own conversation in place** — summarising the oldest turns into a compact
*pointer index* grounded in the durable state stores, while keeping the task and
the recent turns verbatim. No agent is ever killed by a context-length `400`
again, and a resumed session reloads the *compacted* history rather than the
unbounded original.

This is the memory-management counterpart to the existing
[`adaptive-audit`](../adaptive-audit/) reflection loop and
[`resume-spec`](../resume-spec/) checkpointing. Adaptive Audit added *direction*;
Coverage Engine added *breadth*; this adds *endurance* — the ability to keep
working a long assessment without the context growing until the endpoint
rejects it.

## Problem (evidence, not opinion)

A resumed deep-scan run (`host-docker-internal-3000_6ed5`) died mid-turn with:

```
openai.BadRequestError: 400 — "This model's maximum context length is 262144
tokens. However ... your prompt contains at least 262145 input tokens ...
(parameter=input_tokens, value=262145)"
```

Log sequence at `11:14:01–02`:

```
context_limit: Context-limit filter learned provider max context = 262144 tokens
execution:     Lowered context budget for b6c5d1d8 to reported max 262144; retrying (1)
openai.agents:  Calling LLM                     ← retry with trimmed context
(400 again — still over)
execution:     agent run failed for b6c5d1d8; parking as failed
```

Root causes, all confirmed against the code:

1. **The session grows unbounded.** The SDK stores each agent's full transcript
   and replays it every turn. [`context_limit.py`](../../strix/core/context_limit.py)
   trims only the *outbound copy* — *"the persisted session on disk is never
   mutated ... each turn re-trims from full history."* So a long-running root
   agent's stored history only ever grows; on resume the full history reloads
   and immediately re-overflows.
2. **No real headroom.** Default `STRIX_LLM_CONTEXT_WINDOW = 262144`
   ([`settings.py:46`](../../strix/config/settings.py)) equals the model's *exact*
   hard limit. The trim target is `window − _RESERVE_TOKENS` (262144 − 16384 =
   245 760), but the token count is a **bytes/4 heuristic**
   (`_APPROX_BYTES_PER_TOKEN = 4`) that only counts `input` items — it does **not**
   count `instructions` (the ~30 KB system prompt sent separately) and
   under-counts token-dense content (JWTs, hashes, base64 loot). So a request
   "trimmed to 245 760 est." was still ≥ 262 145 real tokens → second `400`.
3. **The adaptive back-off can't ratchet.** `note_context_length()` only lowers
   the budget to the provider's *reported* max. The provider keeps reporting
   `262144`, so on the second rejection `reported_max >= learned_window` returns
   `False`, the retry branch is skipped, and the agent is **parked as failed**
   after a single ineffective retry. It reacts to the reported number (which
   never shrinks) instead of shrinking its own budget when its *estimate* is what
   is wrong.

The failure is a systemic architecture gap, not a one-off: any sufficiently long
agent (or any resume of one) hits it.

## Why not "spawn orchestrator_N from a prose summary at 75%"

The proposed alternative — when the orchestrator hits 75%, spawn a successor
seeded with an LLM-written summary, and chain `orchestrator_1`,
`orchestrator_2`, … — was evaluated and rejected as the *primary* mechanism:

- **Root-only.** Overflow hits any agent; child specialists accumulate the
  heaviest tool output (sqlmap dumps, DOM, proxy traffic). Succession does
  nothing for them. The fix must be session-level.
- **Trusts one large LLM summary at the worst moment**, on a possibly-weak model,
  and the summary call can itself `400`. Loss compounds across generations.
- **Redundant with the blackboard.** The "important decisions and findings"
  already persist outside context in `audit_state`, `notes`, `loot`, vuln
  reports, and `agents_graph`. The transcript is largely *reconstructable* from
  structured state — cheaper and higher-fidelity than re-summarising prose.
- **Breaks graph identity/routing.** Children notify parent `b6c5d1d8`;
  `send_message_to_agent` / `agent_finish` / budget / TUI tree hang off it.
  Replacing the root means re-parenting live children.

Succession is kept **only as the escape hatch** (see
[01-architecture.md](01-architecture.md) §7): when compaction genuinely cannot
fit a single agent, hand off to a successor **seeded from a structured
`rehydrate`, not prose**, as a general op available to any agent.

## Design commitments

- **Index, not grep.** The stores are already ID-addressable with metadata-first
  listing (`list_notes` returns titles/tags, `get_note(id)` returns the body;
  `qa_loot_summary`; `audit_state` snapshot). Compaction preserves an **index of
  pointers** (IDs + one-line labels + status) in context and drops only verbatim
  bulk; the full record stays fetchable by ID. Retrieval is deterministic lookup,
  never fuzzy search. **No vector DB / embeddings** — per-run stores are small
  and bounded.
- **Reuse, don't rebuild.** `compact_session` sits beside the existing in-place
  session mutators `strip_all_images_from_session` /
  `repair_malformed_tool_calls_in_session` in
  [`sessions.py`](../../strix/core/sessions.py) and uses the same
  `get_items → clear_session → add_items` pattern. Seed summaries from the
  existing stores; reuse `trim_items`' orphan-dropping; make the summary call the
  way [`reflection.py`](../../strix/core/reflection.py) makes its structured call
  (a code path, not an agent).
- **Mutate the persisted session.** Compaction rewrites stored history so growth
  is actually bounded and resume reloads the compacted version.
- **Honest meter first.** Every trigger depends on an accurate fill estimate;
  Layer 0 fixes the meter and adds real headroom before anything relies on it.
- **Never kill on a recoverable size error.** A context-length `400` triggers
  compaction + retry with progressive back-off, never park-as-failed.
- **Preserve tool-call integrity.** `function_call` ↔ `function_call_output`
  pairs (matched by `call_id`) are never split across the compaction boundary.

## Features / layers (build order)

| # | Layer | Surface | What it is |
|---|-------|---------|------------|
| 0 | **Honest meter + margin** | `context_limit.py`, `settings.py` | Accurate token estimate incl. instructions; percentage headroom; default window below model max. *Also fixes the live 400.* |
| 1 | **Rolling in-place compaction** | `sessions.py: compact_session` | Summarise the oldest span into a pointer-index memory item; keep task + recency window verbatim; rewrite the stored session. All agents. |
| 2 | **Blackboard rehydrate** | `sessions.py`/`reflection.py: rehydrate_working_context` | Rebuild a minimal context deterministically from the stores (`list`/`get`); auto-inject the index into retained context. |
| 3 | **Thin root** | `agents_graph/tools.py` | Cap the size of child `result_summary` ingested into the parent's context (digest + IDs; full detail in loot/reports). |
| 4 | **Graceful back-off** | `execution.py` | Pre-flight fit check; progressive multiplicative budget shrink even when reported-max is unchanged; compaction-only turn as last resort; never park on a recoverable size 400. |
| 5 | **Successor handoff (fallback)** | `agents_graph`/`sessions.py` | Only when compaction cannot fit: spawn a successor seeded from `rehydrate`. General op, not root-only. |

See [01-architecture.md](01-architecture.md) for the detailed design,
[02-task-list-and-tests.md](02-task-list-and-tests.md) for the ordered tasks and
tests, and [03-handover-prompt.md](03-handover-prompt.md) for the developer-agent
brief.
