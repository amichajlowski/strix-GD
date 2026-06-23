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
- Avoid storing secrets, PII, tokens, raw credentials, or client identifiers in new error metadata.
  If an error contains sensitive values, redact them as `XXXX`.
- Prefer selectable TUI text over a copy button. Add a copy action only if terminal selection is not
  reliable for a specific field.

## Specification Order

1. `01-tui-failed-agent-recovery.md`
2. `02-early-checkpoint-and-same-run-restart.md`
3. `03-resume-state-integrity-and-repair.md`
4. `04-durable-sources-and-evidence.md`
5. `05-child-agent-error-propagation.md`

The first two specs unblock the user's primary pain: a failed red TUI state with no practical way
to retry or resume. The later specs reduce the number of cases that become unrecoverable.

## Cross-Spec Contract

All specs use the same lightweight error record in agent metadata:

```json
{
  "last_error": {
    "type": "RuntimeError",
    "message": "short redacted message",
    "cause": "human-readable likely cause",
    "suggested_fix": "one concise action",
    "recoverable": true,
    "occurred_at": "ISO-8601 timestamp"
  }
}
```

Only `type`, `message`, and `occurred_at` are required. The rest are best effort.

Run-level recovery statuses are plain `run.json` strings, not a new enum:

- `paused`: state is intentionally saved for later resume.
- `cancelled_findings_saved`: agent replay state is intentionally abandoned, but findings and run
  metadata are retained.

Agent statuses keep the existing coordinator values: `running`, `waiting`, `completed`, `stopped`,
`crashed`, and `failed`.
