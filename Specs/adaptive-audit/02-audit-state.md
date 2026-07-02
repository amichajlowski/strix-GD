# Feature 1 — Audit State Store

**Priority: build first. Independently useful; the strategist loop (Feature 2)
writes into it.**

## What it does

A single per-run **working thesis** document, shared by every agent and
persisted to `{state_dir}/audit_state.json`. It is where the evolving theory of
the target lives: a one-paragraph thesis, a set of **assumptions with confidence
and supersede history**, and a list of **prioritised leads**. This is the
synthesis/direction layer on top of the raw stores (`loot`, `target_profile`,
`notes`).

It deliberately covers both original asks: **#1 audit memory** (the thesis +
leads) and **#3 assumption revision** (assumptions carry confidence and are
changed by supersede, not overwrite). Revision is a property of this store, so
there is no separate machinery — a smaller surface on purpose.

Model the module on `strix/tools/notes/tools.py` (dict + `RLock` + atomic
persist + hydrate), except the store holds **one document**, not a keyed
collection.

## Document schema

```python
{
  "thesis": "Laravel API behind Cloudflare; auth is bearer-token; best leads are IDOR on /orders and a leaked staging key.",  # bounded
  "assumptions": {
    "a1b2c3": {
      "assumption_id": "a1b2c3",
      "text": "No WAF in front of the origin",   # bounded
      "confidence": "medium",                      # low | medium | high
      "status": "superseded",                      # active | superseded
      "supersedes": None,                          # id this one replaced, or None
      "superseded_by": "d4e5f6",                   # set when a newer one replaces it
      "reason": "initial httpx run showed no challenge",  # bounded
      "refs": ["<loot_id|note_id>"],               # evidence, bounded list (ids only)
      "created_at": "...isoformat...",
      "updated_at": "...isoformat...",
    }
  },
  "leads": {
    "l7g8h9": {
      "lead_id": "l7g8h9",
      "text": "IDOR on /orders/{id} using object_id from loot",  # bounded
      "priority": "high",                          # low | medium | high
      "status": "open",                            # open | in_progress | done | dropped
      "rationale": "sequential ids seen in traffic",  # bounded
      "refs": ["<loot_id|note_id>"],               # bounded list (ids only)
      "created_at": "...isoformat...",
      "updated_at": "...isoformat...",
    }
  },
  "updated_at": "...isoformat...",
}
```

Enums (validate; reject unknown with the valid set, mirroring
`_VALID_LOOT_TYPES`):

- `confidence`: `low | medium | high`
- `priority`: `low | medium | high`
- `lead status`: `open | in_progress | done | dropped`
- `assumption status`: `active | superseded` (set by the store, not the caller)

## Tools (keep the surface minimal — two tools)

```python
@function_tool(timeout=30)
async def get_audit_state(ctx, include_superseded: bool = False) -> str: ...

@function_tool(timeout=30)
async def update_audit_state(
    ctx,
    thesis: str | None = None,
    assumption: str | None = None,      # add (or supersede) one assumption by text
    confidence: str | None = None,      # confidence for `assumption`
    supersedes: str | None = None,      # assumption_id this assumption replaces
    reason: str | None = None,          # why (recorded on the new/superseding assumption)
    lead: str | None = None,            # add one lead by text
    priority: str | None = None,        # priority for `lead`
    lead_id: str | None = None,         # update an existing lead...
    lead_status: str | None = None,     # ...to this status
    refs: list[str] | None = None,      # evidence ids for the assumption/lead being written
) -> str: ...
```

Two tools, flat SDK-clean params (no nested dicts — the SDK cannot schema those
under strict mode). One `update_audit_state` call may set the thesis and/or add
one assumption and/or add one lead and/or update one lead's status; the
strategist typically calls it a few times per reflection. Overloaded on purpose
to keep the base-tool count (and prompt-token cost) down — see the 01 scope note.

> **SDK strict-schema note.** Every param must keep the `T | None = None` form
> (as `set_target_profile` does): strict mode marks all params required-nullable,
> so a param without a `None` default breaks schema generation. Do not "simplify"
> by dropping `| None`.
>
> **Overload wart (call out at the param site, not just here):** a lead's
> *rationale* is passed through the shared `reason` param (the same param that
> carries an assumption's supersede reason). One `update_audit_state` call should
> therefore write **either** an assumption **or** a lead, not both, when `reason`
> is set — document this in the docstring so the model doesn't cross them.

- **`get_audit_state`** — return `thesis`, `assumptions` (active only unless
  `include_superseded`), `leads` (open/in_progress first), and `updated_at`.
  Never errors on an empty store — returns an empty thesis and empty lists.
- **`update_audit_state`** — apply exactly the provided pieces:
  - `thesis` → replace the thesis (bounded).
  - `assumption` (+ `confidence`, optional `supersedes`/`reason`/`refs`) → create
    a new assumption. **If `supersedes` is set**, the target assumption is marked
    `status="superseded"`, `superseded_by=<new id>`, and the new one records
    `supersedes=<old id>` + `reason`. Reject `supersedes` pointing at an unknown
    or already-superseded id (clear error).
  - `lead` (+ `priority`, optional `rationale` via `reason`, `refs`) → create a
    new lead (`status="open"`).
  - `lead_id` (+ `lead_status`, optional `priority`) → update an existing lead;
    unknown id → clean error.
  - Require `confidence` when `assumption` is set, and `priority` when `lead` is
    set (reject with a clear message otherwise).
  - Bound every string; cap collection sizes. Bump `updated_at`.
  - **Scrub free text at write time:** run `scrub_secrets(...)` over `thesis`,
    `assumption`, `lead`, and `reason` before storing (cheap defense-in-depth for
    the ids-only convention — see 01 §Secret discipline). Silently degrades a
    leaked value to `XXXX`; never rejects the write.
  - Return `{"success", "assumption_id"?, "lead_id"?, "counts": {...}}`.

## Pure helpers (the unit-test targets)

```python
def _apply_assumption(state, *, text, confidence, supersedes, reason, refs) -> tuple[dict, str]:
    """Return (new_state, assumption_id). Handles the supersede transition. Pure."""

def _apply_lead(state, *, text, priority, rationale, refs) -> tuple[dict, str]: ...
def _update_lead(state, *, lead_id, status, priority) -> dict: ...        # pure; raises/returns error marker on unknown id

def qa_audit_summary(state, limit: int = 100) -> dict[str, Any]:
    """refs = open leads (lead_id, priority, status) + active assumptions
    (assumption_id, confidence) — **ids/enums only, NO free text** (refs are
    persisted into the QA review → run.json/bundle, so they match
    qa_loot_summary's id-only refs). signals = lowercased thesis/lead/assumption
    text, `scrub_secrets`-cleaned, for QA rule inspection only — in-memory,
    never persisted. No raw values (there should be none; scrub defensively)."""
```

Keep helpers pure (take `state`, return new state / derived data) so revision
and reconciliation are tested without any tool wrapper, LLM, or disk. Follow the
immutability convention — return new dicts, do not mutate the argument.

## Bounds (no unbounded growth)

- `thesis` ≤ 1000 chars; `text`/`reason`/`rationale` ≤ 512; each ref ≤ 64;
  `refs` ≤ 32 per entry.
- Active assumptions ≤ 200, leads ≤ 200. Superseded assumptions are retained for
  the audit trail but the **total** (active + superseded) is capped (e.g. 1000);
  past the cap, drop the oldest superseded first. `get_audit_state` default view
  excludes superseded, so context stays small.

## Secret discipline (mandatory — see 01 §Secret discipline)

- `audit_state` is **derived intel only**. Never store a raw secret value —
  reference loot by `loot_id`; use `loot.mask_value` if a hint is unavoidable.
- **Write-time scrub backstop:** `update_audit_state` runs `scrub_secrets` over
  `thesis`/`assumption`/`lead`/`reason` (see the behaviour bullet above). One
  call per field — the proportionate belt; no write-rejection or loot
  cross-referencing.
- Notes-style atomic persist (`0644`); no `0o600` (no raw secrets). Same
  bundle/transcript in-scope caveat as `target_profiles.json`.
- `qa_audit_summary` is what the QA gate consumes: **`refs` are ids/enums only
  (no free text — they get persisted into the QA review)**; free text lives only
  in the in-memory `signals` (scrubbed), never persisted. Matches
  `qa_loot_summary` exactly.

## Wiring

- Register `get_audit_state`, `update_audit_state` in `_BASE_TOOLS`
  (`factory.py`) with imports.
- `hydrate_audit_state_from_disk(state_dir)` in `runner.py` next to the other
  hydrations.
- Add the per-file-ignore `"strix/tools/audit_state/tools.py" = ["PLC0415",
  "TC002"]` in `pyproject.toml`.
- Shared prompt line (01 §5): read `get_audit_state` before starting a new
  surface.

## Acceptance criteria

- `update_audit_state` creates/updates thesis, assumptions, and leads; enum
  validation rejects unknown `confidence`/`priority`/`lead_status` with the valid
  set listed.
- Supersede works: superseding assumption `X` marks `X` superseded + links both
  ways with a reason; superseding an unknown/already-superseded id errors
  cleanly.
- `get_audit_state` hides superseded by default, shows them with
  `include_superseded=True`, and never errors on an empty store.
- All strings/collections bounded; the superseded-history cap evicts oldest.
- `_persist()` → `hydrate_audit_state_from_disk` round-trips; malformed
  `audit_state.json` hydrates to an empty document without raising.
- `qa_audit_summary` `refs` carry **ids/enums only** (no `text` field); free text
  appears only in `signals` (scrubbed, in-memory). No raw values anywhere.
- No raw secret value can be stored (documented + covered by a test that a
  loot-value-looking string is the caller's responsibility — the store does not
  fetch loot; it only stores what it is given, so the guard is the strategist
  skill + the "ids only" convention, tested via `qa_audit_summary` carrying no
  value field).
