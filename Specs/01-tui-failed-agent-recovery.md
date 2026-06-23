# TUI Failed Agent Recovery

## Problem

Interactive audits can show a red failed state with no useful next action. The current TUI can show
`error_message`, but graph sync only supplies agent id, name, parent, and status. Root failures also
close the scan loop, so TUI messages cannot wake the root orchestrator after failure.

## Current Code Alignment

- `strix/interface/tui/app.py` has a status display for `failed`, but it usually falls back to
  `Scan failed`.
- `strix/interface/tui/messages.py` can only deliver messages while the scan loop is open.
- `strix/core/execution.py` re-raises interactive root `failed` and `crashed` statuses.
- `strix/core/agents.py` already snapshots `metadata`; use that for structured failure details.
- `strix/tools/agents_graph/tools.py` already documents that messages can wake stopped or failed
  agents when their SDK session is attached.
- Current status display text is not enough for recovery details; render detailed errors in a
  selectable modal, panel, or chat-style surface before deciding whether a copy action is needed.

## Functional Scope

Add a TUI recovery state for failed, crashed, or stopped agents that still have an attached session.
The user sees the cause, suggested fix, and clear actions:

- Retry selected agent
- Save state for later resume
- Cancel audit and keep findings

Use a selectable error surface by default. Add a small copy action only if selection is unreliable
in the chosen Textual surface.

## Non-Goals

- No separate incident export bundle.
- No new persistence backend.
- No exact replay of an individual failed tool call. Retry means "resume the selected agent with a
  retry instruction".

## User Flow

1. Agent fails, crashes, or is stopped by an error that can be retried or restarted.
2. TUI keeps the app open and selects the failed agent.
3. Status panel shows:
   - agent name and id
   - status
   - exception type
   - scrubbed message
   - likely cause if known
   - suggested fix
4. User chooses one of:
   - Retry: send a retry instruction to the selected agent.
   - Save for resume: persist state and exit cleanly.
   - Cancel, keep findings: persist findings and run record, remove agent replay state.

## Ordered Tasks

1. Add a minimal structured-secret scrubber for recovery metadata, as defined in `README.md`.
2. Add `AgentCoordinator.record_error(agent_id, exc, *, cause=None, suggested_fix=None,
   recoverable=True)` that writes `metadata[agent_id]["last_error"]`.
   - capture exception type, status code when available, and a bounded scrubbed message prefix
   - do not persist full provider responses, request bodies, headers, cookies, or credentials
3. Add `AgentCoordinator.clear_error(agent_id)` and call it from `mark_running()`.
4. Add a graph snapshot method that includes metadata without breaking existing callers, for example
   `graph_snapshot_with_metadata()`.
5. In `strix/core/execution.py`, record structured errors before setting `failed`, `crashed`, or
   the `MaxTurnsExceeded` error-driven `stopped` status.
6. Change interactive root failure handling so root is parked as `failed` or `crashed` without
   re-raising while the TUI is active. This keeps the scan loop alive.
   This covers agent-run failures; sandbox startup retry is handled in
   `02-early-checkpoint-and-same-run-restart.md`.
7. Update TUI graph sync to hydrate `last_error` into `TuiLiveView.upsert_agent()`.
8. Update the TUI status display for `failed`, `crashed`, and error-driven `stopped` states.
9. Add a small recovery screen or action bar using existing TUI modal patterns:
   - Retry
   - Save for resume
   - Cancel, keep findings
10. Implement Retry by sending a user instruction to the selected agent:
   - mention the previous error type and message
   - ask the agent to retry from the last safe point
   - do not repeat secrets or raw target credentials
11. Implement Save for resume by writing `run.json.status = paused`, preserving `.state`, and
    closing the TUI cleanly. Ensure later cleanup calls do not downgrade `paused` to `stopped`.
12. Implement Cancel, keep findings by:
    - writing `run.json.status = cancelled_findings_saved`
    - saving findings and run metadata
    - disabling further coordinator snapshot writes or stopping the coordinator
    - then deleting or ignoring `.state/agents.json` and `.state/agents.db`
13. Keep existing `Escape` stop-agent behaviour for running and waiting agents.

## Test Cases

Add focused tests before broad UI refactors. Prefer coordinator and TUI helper tests over full
terminal integration where possible.

1. `tests/test_agent_errors.py::test_record_error_persists_scrubbed_metadata`
   - Create a coordinator and registered agent.
   - Record exceptions containing Authorization/Bearer, cookie, password, JWT, and basic-auth URL
     values.
   - Assert `metadata[agent_id]["last_error"]` exists, structured secrets are `XXXX`, non-secret
     text remains useful, and the message is length-bounded.
2. `tests/test_agent_errors.py::test_mark_running_clears_last_error`
   - Record an error.
   - Call `mark_running()`.
   - Assert `last_error` is removed and status is `running`.
3. `tests/test_execution.py::test_interactive_root_failure_parks_without_reraising`
   - Mock `Runner.run_streamed()` to raise a runtime exception.
   - Run `_run_cycle()` with root context and `interactive=True`.
   - Assert no exception escapes, root status is `failed` or `crashed`, and metadata has
     `last_error`.
4. `tests/test_tui_recovery.py::test_graph_sync_hydrates_error_metadata`
   - Provide a graph snapshot with metadata.
   - Assert `TuiLiveView.upsert_agent()` stores the error message and type.
5. `tests/test_tui_recovery.py::test_failed_status_renders_recovery_prompt`
   - Build failed agent data with `last_error`.
   - Assert the status display contains the exception type, message, and suggested fix.
6. `tests/test_tui_recovery.py::test_retry_failed_agent_sends_retry_instruction`
   - Select a failed agent with attached session.
   - Trigger retry.
   - Assert `coordinator.send()` is called with a user instruction and the agent is wakeable.
7. `tests/test_tui_recovery.py::test_save_for_resume_preserves_agent_state`
   - Trigger Save for resume.
   - Assert `run.json`, `.state/agents.json`, `.state/agents.db`, and findings remain.
8. `tests/test_tui_recovery.py::test_cancel_keep_findings_removes_replay_state_only`
   - Trigger Cancel, keep findings.
   - Assert findings remain, `run.json.status` is `cancelled_findings_saved`, cleanup does not
     downgrade the status, and agent replay state is absent or ignored on the next launch.
9. `tests/test_tui_recovery.py::test_cancel_keep_findings_cannot_same_run_restart`
   - Cancel a run and then attempt `--resume`.
   - Assert Strix refuses same-run restart because `cancelled_findings_saved` is present.
10. `tests/test_tui_recovery.py::test_save_for_resume_status_survives_cleanup`
   - Save for resume and invoke the existing cleanup path.
   - Assert `run.json.status` remains `paused`.

## Regression Checks

- Existing running and waiting agents still show the same controls.
- Sending ordinary chat messages still works for running and waiting agents.
- Failed child agent can be selected and retried.
- Failed root agent leaves the TUI open and can be retried.
- Save for resume leaves `run.json`, findings, `agents.json`, and `agents.db`, with status
  `paused`.
- Cancel, keep findings leaves vulnerability artefacts and does not offer normal resume.
- Structured secrets are not copied into `last_error`.
