# Early Checkpoint And Same-Run Restart

## Problem

If a scan fails before the first agent snapshot is written, `--resume` refuses to continue because
`.state/agents.json` is missing. This can happen during sandbox startup, Caido bootstrap, or other
early runtime setup. The run may still have `run.json` and findings, but the user is forced to start
again under a new run.

## Current Code Alignment

- `strix/core/runner.py` creates the run and state directories before sandbox startup.
- Root agent registration currently happens after sandbox startup.
- `strix/interface/main.py` rejects `--resume` when `.state/agents.json` is missing.
- `ReportState.hydrate_from_run_dir()` already preserves prior findings and run metadata.

## Functional Scope

Create a usable checkpoint before sandbox startup and allow same-run restart when only the agent
snapshot is missing.
Use explicit runner modes: `fresh`, `resume`, and `same_run_restart`.

## Non-Goals

- Do not reconstruct full SDK conversation history when `agents.db` never existed.
- Do not invent agent history. If the SDK session is missing, restart the root with a concise
  recovery instruction based on `run.json` and existing findings only.

## User Flow

1. User starts a scan.
2. Strix writes `run.json` and a root agent checkpoint before sandbox startup.
3. If startup fails, TUI/CLI offers:
   - Retry startup now
   - Save for resume later
   - Restart same run from saved config
4. If `agents.json` is missing but `run.json` is valid, `--resume` uses same-run restart instead of
   hard failing.

## Ordered Tasks

1. In `run_strix_scan()`, compute `targets`, `scan_mode`, `skills`, `root_task`, and `root_id`
   before sandbox startup.
2. Initialise `AgentCoordinator`, set the snapshot path, register root, and snapshot before
   `session_manager.create_or_reuse()`.
3. Ensure fresh runs do not double-register root after moving the registration earlier.
4. Move `session_manager.create_or_reuse()` inside the protected `try` block so sandbox/Caido
   startup failures can record root error metadata and run normal cleanup/report handling.
5. If sandbox startup fails after the early checkpoint, record the error on the root metadata.
6. Change the CLI resume gate for missing `agents.json`:
   - if `run.json` is missing or invalid, keep hard failure
   - if `run.json.status` is `cancelled_findings_saved`, refuse restart and tell the user to start
     a fresh run
   - if `run.json` is valid and not cancelled, allow same-run restart
7. Same-run restart should:
   - keep the same run directory
   - preserve findings
   - create a new root SDK session if `agents.db` is absent
   - restart root only when SDK history is unavailable; do not respawn children into empty sessions
   - add a recovery instruction explaining that previous agent history was unavailable
8. Add a TUI action for "Retry startup" / "Restart same run" when the scan failed before session
   attachment. The action should start a new scan thread and re-enter `run_strix_scan()` for the
   same run name rather than trying to message an agent in a closed event loop.
9. Add logging that distinguishes `fresh`, `resume`, and `same_run_restart`.

## Test Cases

1. `tests/test_runner_resume.py::test_fresh_scan_writes_root_snapshot_before_sandbox_start`
   - Mock `session_manager.create_or_reuse()` to raise.
   - Run `run_strix_scan()` far enough to fail startup.
   - Assert `.state/agents.json` exists with one root agent.
2. `tests/test_runner_resume.py::test_fresh_scan_registers_root_once`
   - Run a successful mocked fresh scan.
   - Assert the coordinator snapshot contains exactly one root agent.
3. `tests/test_runner_resume.py::test_startup_failure_records_root_error`
   - Mock sandbox startup failure.
   - Assert root metadata contains `last_error` with the failure type and scrubbed message.
4. `tests/test_cli_resume.py::test_resume_missing_agents_json_with_valid_run_json_allows_restart`
   - Create `run.json` without `.state/agents.json`.
   - Parse `--resume`.
   - Assert args are populated from `run.json` and marked for same-run restart.
5. `tests/test_cli_resume.py::test_resume_cancelled_findings_saved_refuses_restart`
   - Create `run.json` with status `cancelled_findings_saved` and no `agents.json`.
   - Parse `--resume`.
   - Assert same-run restart is refused with an actionable message.
6. `tests/test_cli_resume.py::test_resume_missing_run_json_still_fails`
   - Run `--resume` for a missing run.
   - Assert parser error remains.
7. `tests/test_runner_resume.py::test_same_run_restart_preserves_existing_findings`
   - Create prior `vulnerabilities.json`.
   - Restart same run.
   - Assert the next report id does not overwrite previous findings.
8. `tests/test_runner_resume.py::test_same_run_restart_creates_root_only_session_when_db_missing`
   - Create valid `run.json`, valid `agents.json` with children, and missing `agents.db`.
   - Run same-run restart path.
   - Assert a new root session is opened, a recovery instruction is inserted, and child agents are
     not respawned into empty sessions.
9. `tests/test_tui_recovery.py::test_retry_startup_restarts_scan_thread`
   - Mock sandbox startup failure in TUI mode.
   - Trigger Retry startup.
   - Assert a new scan thread is started for the same run name.

## Regression Checks

- Normal fresh scans still create exactly one root agent.
- Normal `--resume` with valid `agents.json` and `agents.db` still replays SDK history.
- Missing `run.json` remains a hard resume failure.
- Missing `agents.json` with valid `run.json` becomes repairable.
- `cancelled_findings_saved` is not repairable through same-run restart.
- Existing findings are not renumbered or overwritten.
- Sandbox startup failures produce a visible root error record.
