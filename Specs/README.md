# Resume And Recovery Specifications

This folder contains focused specifications for making Strix audits easier to continue after
runtime, model, agent, or environment failures. The intent is functional recovery with minimal
code: use the existing run directory, coordinator snapshots, SDK sessions, report state, and TUI
patterns wherever possible.

## Shared Principles

- Keep resumability centred on the existing run directory:
  - `run.json`
  - `.state/agents.json`
  - `.state/agents.db`
  - existing findings artefacts
- Do not introduce a separate persistence service.
- Do not add duplicate agent state stores. Extend `AgentCoordinator.metadata` only where needed.
- Preserve current successful scan behaviour.
- Never discard vulnerability reports during recovery, pause, or cancellation flows.
- Prefer selectable TUI text over a copy button. Add a copy action only if terminal selection is not
  reliable for a specific field.

## Testing Principle

Tests should guard only the paths where users lose findings, cannot continue an audit, or could
persist structured secrets to disk. Prefer coordinator, runner, and TUI helper tests with mocked
runner/session-manager boundaries.

Do not add terminal/Textual pilot end-to-end tests, numeric coverage gates, action-bar render tests,
or standalone graph snapshot tests unless implementation risk justifies them. Do not add a manifest
idempotency test while the manifest has a single writer by construction. Do not add an Escape-stop
regression test unless the running/waiting stop path is modified.

## Specification Order

1. `01-tui-failed-agent-recovery.md`
2. `02-early-checkpoint-and-same-run-restart.md`
3. `03-resume-state-integrity-and-repair.md`
4. `04-durable-sources-and-evidence.md`
5. `05-child-agent-error-propagation.md`

The first two specs unblock the user's primary pain: a failed red TUI state with no practical way
to retry or resume. The later specs reduce the number of cases that become unrecoverable.

## Cross-Spec Contract

### Run Modes

Runner code should use explicit run modes instead of overloading the current `is_resume` boolean:

- `fresh`: new run, register root, use the normal root task as initial input.
- `resume`: valid `agents.json` and `agents.db`, restore topology and SDK sessions.
- `same_run_restart`: valid `run.json`, but full SDK replay is unavailable or intentionally not
  used; restart root only with a concise recovery instruction.

### Error Metadata

All specs use the same lightweight error record in agent metadata:

```json
{
  "last_error": {
    "type": "RuntimeError",
    "message": "short scrubbed message",
    "status_code": 500,
    "cause": "human-readable likely cause",
    "suggested_fix": "one concise action",
    "recoverable": true,
    "occurred_at": "ISO-8601 timestamp"
  }
}
```

Only `type`, `message`, and `occurred_at` are required. The rest are best effort. `recoverable` is
display guidance only; retry availability is determined by whether the agent has an attached SDK
session or the run can be restarted.

Capture as little as possible:

- store `type(exc).__name__`
- store `getattr(exc, "status_code", None)` when present
- store only a bounded message prefix after scrubbing
- do not persist full provider response bodies, HTTP bodies, request headers, cookies, or raw
  credentials

### Structured Secret Scrubbing

No general redaction utility exists today. Implement a small helper for new recovery metadata and
evidence text. It should replace common structured secrets with `XXXX`:

- `Authorization` / `Bearer` values
- `Cookie` / `Set-Cookie` values
- query or form values named like `api_key`, `token`, `password`, `secret`, or `credential`
- basic-auth URLs such as `https://user:pass@example.test`
- JWT-shaped values
- common cloud key shapes such as AWS access key ids

Do not claim general PII or arbitrary client-name detection. Paths and workspace mappings should
remain readable unless they contain credential-bearing values.

### TUI Metadata Plumbing

Add a metadata-aware graph snapshot path, such as `graph_snapshot_with_metadata()`, that carries
agent metadata and checkpoint warnings without breaking existing `graph_snapshot()` callers. Extend
`TuiLiveView.upsert_agent()` to store structured `last_error` and checkpoint warnings, not only a
flat `error_message`.

Run-level recovery statuses are plain `run.json` strings, not a new enum:

- `paused`: state is intentionally saved for later resume.
- `cancelled_findings_saved`: agent replay state is intentionally abandoned, but findings and run
  metadata are retained.

These statuses must be protected from later `cleanup(status="stopped")` calls. Resume/restart gates
must read `run.json.status`; `cancelled_findings_saved` must not silently enter same-run restart.

Agent statuses keep the existing coordinator values: `running`, `waiting`, `completed`, `stopped`,
`crashed`, and `failed`.

An error-driven `stopped` agent is defined by status `stopped` plus a `last_error` record.
