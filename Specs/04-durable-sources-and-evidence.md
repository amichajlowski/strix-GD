# Durable Sources And Evidence

## Problem

Repository clones are stored under temporary paths. Resume can fail when the temp directory is
cleaned. Sandbox cleanup also removes runtime evidence that may help recovery or audit continuity.

## Current Code Alignment

- `clone_repository()` stores clones under a temp directory.
- `_load_resume_state()` hard fails if the recorded clone path is missing.
- `session_manager.cleanup()` tears down the sandbox and swallows cleanup errors.
- Findings are already persisted when reports are created.

## Functional Scope

Make source inputs and essential evidence durable enough for resume and investigation without
copying excessive sandbox state.

## Non-Goals

- Do not persist full container filesystems.
- Do not persist browser profiles or secrets.
- Do not archive arbitrary target data.

## Ordered Tasks

1. Change repository clone destination from temp storage to `strix_runs/<run_name>/sources/<name>`.
2. Keep existing workspace subdirectory naming rules so container paths remain stable.
3. Move resume clone repair out of argument parsing. `_load_resume_state()` should detect the
   missing source and mark it repairable; the actual network re-clone happens in `main()` after
   parsing.
4. On resume, if a run-owned clone is missing, re-clone into the same run-owned location.
5. If a user-provided local path or mount is missing, show a repairable error with the exact missing
   path and suggested fix.
6. Before sandbox cleanup, persist a small evidence manifest under the run directory from a single
   owner, preferably `run_strix_scan()`'s `finally` block where `run_dir`, source mapping, and the
   sandbox bundle are in scope:
   - Caido/proxy export path if available
   - known generated report files
   - workspace source mapping
   - sandbox cleanup status
   Manifest writing must be idempotent and failures must be logged without blocking cleanup or
   findings persistence.
7. Add proxy history export only if the current Caido client API supports it with a small, direct
   call. Otherwise record the Caido project URL and leave implementation for a separate task.
8. Scrub structured secrets in persisted evidence text using the shared helper. Keep source paths
   and workspace mappings readable unless they contain credential-bearing values.

## Test Cases

1. `tests/test_local_sources.py::test_repository_clone_uses_run_owned_sources_dir`
   - Mock `git clone`.
   - Assert destination is under `strix_runs/<run_name>/sources/<name>`.
2. `tests/test_local_sources.py::test_resume_reclones_missing_run_owned_repository`
   - Create `run.json` with repository target and missing run-owned clone.
   - Assert resume re-clones to the same run-owned path.
3. `tests/test_local_sources.py::test_missing_clone_repair_is_not_performed_in_argparse`
   - Create `run.json` with a missing run-owned clone.
   - Assert parsing marks a repairable condition but does not run `git clone`.
4. `tests/test_local_sources.py::test_resume_missing_user_local_path_is_repairable_error`
   - Create `run.json` with a local source path that no longer exists.
   - Assert the error names the missing path and suggests restore or update.
5. `tests/test_session_entries.py::test_workspace_subdir_mapping_stays_stable`
   - Verify run-owned clone paths still map to the same `/workspace/<subdir>` entries.
6. `tests/test_runtime_evidence.py::test_runner_finally_writes_evidence_manifest`
   - Mock a session bundle and cleanup.
   - Assert an evidence manifest is written by the runner before cleanup.
7. `tests/test_runtime_evidence.py::test_evidence_manifest_is_idempotent`
   - Invoke cleanup/finally paths more than once.
   - Assert the manifest is not duplicated or corrupted.
8. `tests/test_runtime_evidence.py::test_cleanup_manifest_scrubs_structured_secrets`
   - Include token, cookie, and credential-like values in mocked evidence.
   - Assert output contains `XXXX` instead of raw values.
   - Assert source paths and workspace mappings remain readable.
9. `tests/test_runtime_evidence.py::test_cleanup_failure_does_not_block_findings_save`
   - Mock container deletion failure.
   - Assert findings and `run.json` are still present.

## Regression Checks

- Existing repository targets still clone before scan.
- Local directory targets and `--mount` still work.
- Resume no longer depends on temp clone paths.
- Missing local source gives a repairable message, not a generic crash.
- Cleanup remains best effort and does not block saving findings.
- Evidence manifest does not include API keys, bearer tokens, cookies, or credentials, while keeping
  source mapping usable for resume.
