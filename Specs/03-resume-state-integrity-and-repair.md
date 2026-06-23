# Resume State Integrity And Repair

## Problem

Resume is blocked when `agents.json` is corrupt or `agents.db` is missing. Coordinator snapshot
write failures are currently logged but not surfaced to the user, so the audit may look resumable
when it is not.

## Current Code Alignment

- `AgentCoordinator._maybe_snapshot()` writes `.state/agents.json` atomically.
- Snapshot write exceptions are logged and swallowed.
- `run_strix_scan()` reads only `.state/agents.json` and requires `.state/agents.db`.
- TUI history hydration already tolerates some read failures, but runner resume does not.

## Functional Scope

Add a simple repair path:

- keep one previous good snapshot
- surface degraded checkpoint health
- resume from backup if the latest snapshot is corrupt and it is safe to replay
- fall back to same-run restart if the SDK database is missing

## Non-Goals

- No timeline database.
- No unlimited snapshot archive.
- No manual JSON editor in the TUI.

## Ordered Tasks

1. Update `_maybe_snapshot()` to keep `.state/agents.previous.json` before replacing
   `.state/agents.json`.
   A plain previous-file copy is acceptable; do not add snapshot management unless profiling proves
   it is needed.
2. Add a helper to load the newest valid snapshot:
   - try `agents.json`
   - if invalid, try `agents.previous.json`
   - return the path used and any warning
3. Use the helper in `run_strix_scan()` resume loading.
4. When falling back to `agents.previous.json`, warn that the snapshot may be older than
   `agents.db`.
   - If SDK session ids can be enumerated, compare them with snapshot agent ids; mismatch routes to
     `same_run_restart`.
   - If session ids cannot be enumerated cheaply, use a simpler safe default: previous-snapshot
     fallback warns and offers root-only same-run restart rather than deep replay.
5. Add a coordinator/report warning field for snapshot write failures.
6. Show checkpoint warnings in the TUI status area through the metadata-aware graph snapshot without
   interrupting a running audit.
7. If `agents.db` is missing but a valid snapshot exists, offer same-run restart rather than full
   resume.
   This is root-only restart; do not respawn children into empty SDK sessions.
8. Add a lightweight `strix doctor --run <name>` command or equivalent subcommand later only if the
   TUI and CLI messages are still insufficient. Do not block this feature on a new command.

## Test Cases

1. `tests/test_agent_snapshots.py::test_snapshot_keeps_previous_good_copy`
   - Write a first snapshot, then a second snapshot.
   - Assert `agents.previous.json` contains the first valid snapshot.
2. `tests/test_agent_snapshots.py::test_resume_uses_latest_valid_snapshot`
   - Create valid `agents.json` and valid previous snapshot.
   - Assert resume loads `agents.json`.
3. `tests/test_agent_snapshots.py::test_resume_falls_back_to_previous_snapshot`
   - Corrupt `agents.json`.
   - Keep `agents.previous.json` valid.
   - Assert the fallback path records a warning and follows the defined safe replay/restart rule.
4. `tests/test_agent_snapshots.py::test_resume_fails_when_all_snapshots_invalid`
   - Corrupt both snapshot files.
   - Assert clear failure with both paths mentioned.
5. `tests/test_agent_snapshots.py::test_previous_snapshot_db_mismatch_routes_to_restart`
   - Create a previous snapshot whose agent ids do not match the available SDK sessions, or mock the
     cheap session-id check as unavailable.
   - Assert Strix offers root-only same-run restart instead of deep replaying stale topology.
6. `tests/test_agent_snapshots.py::test_snapshot_write_failure_sets_checkpoint_warning`
   - Mock filesystem write failure.
   - Assert coordinator/report warning is set and the running audit is not crashed by the warning.
7. `tests/test_runner_resume.py::test_missing_agents_db_routes_to_root_only_same_run_restart`
   - Provide valid snapshot and missing `agents.db`.
   - Assert full SDK replay is not attempted, child respawn is skipped, and restart recovery is
     offered.
8. `tests/test_tui_recovery.py::test_tui_displays_checkpoint_warning`
   - Hydrate a checkpoint warning into TUI state.
   - Assert the warning appears without replacing agent status.

## Regression Checks

- Existing atomic snapshot behaviour remains.
- Backup snapshot is not written if the current snapshot was never valid.
- Corrupt latest snapshot with valid previous snapshot resumes from previous snapshot.
- Corrupt latest and missing/invalid previous snapshot still fails clearly.
- Snapshot write failure produces an actionable warning.
- Missing `agents.db` no longer destroys findings or blocks same-run restart.
- Missing `agents.db` never respawns children into empty sessions.
