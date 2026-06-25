# QA Loop Implementation Plan And Tests

## Implementation Constraints

- Keep changes small and local.
- Preserve current successful scan behaviour outside deep-mode completion.
- Do not revert unrelated working-tree changes.
- Do not persist raw secrets, cookies, tokens, request bodies, or full command outputs.
- Use existing report state and coordinator state.
- Prefer pure helper functions for rules and metrics so tests do not need live LLMs, Docker, or
  Caido.

## Ordered Tasks

### Task 1: Add QA Loop Tool Package

Create:

```text
strix/tools/qa_loop/__init__.py
strix/tools/qa_loop/tool.py
strix/tools/qa_loop/rules.py
```

`tool.py` owns the function tool and state collection.

`rules.py` owns pure rule evaluation:

```python
def evaluate_qa_gaps(review_context: dict[str, Any]) -> list[dict[str, Any]]:
    ...
```

Acceptance criteria:

- root agent can call `review_before_finish`
- subagents get a structured error
- no live LLM, Docker, network, or Caido dependency is required for unit tests

### Task 2: Add Review Persistence To ReportState

Update `strix/report/state.py`.

Add:

```python
def record_qa_review(self, review: dict[str, Any]) -> None:
    ...

def get_latest_qa_review(self) -> dict[str, Any] | None:
    ...
```

Store review at:

```python
self.run_record["qa_review"]
```

Hydration should work automatically through existing `run.json` loading.
`set_scan_config()` should not clear an existing `qa_review` on resume. Reuse the persisted review
when the cheap stale metrics still match, and re-block completion when the metrics changed. Fresh
runs use a new run directory, so stale reviews should not cross run boundaries.

Acceptance criteria:

- review is written to `run.json`
- resumed runs can read prior review
- `set_scan_config()` clears stale final scan results as it does today, but does not delete a
  matching prior `qa_review` during resume

### Task 3: Pass QA Loop Context Through Runner

Update `strix/core/runner.py`.

Add to the root context dict in `runner.py`:

```python
"scan_mode": scan_mode,
"qa_loop_enabled": scan_mode == "deep",
```

Children inherit this context automatically through `dict(parent_ctx)` in `_start_child_runner()`.
Do not edit `execution.py` solely to add these fields to children.

Acceptance criteria:

- deep scans enforce the gate
- quick/standard scans are not blocked by missing QA review

### Task 4: Register The Tool For Root Agents Only

Update `strix/agents/factory.py`.

Import `review_before_finish` and include it only in the root tool list:

```python
if is_root:
    tools = [*_BASE_TOOLS, review_before_finish, finish_scan]
else:
    tools = [*_BASE_TOOLS, agent_finish]
```

Acceptance criteria:

- root tools include `review_before_finish`
- child tools do not include `review_before_finish`

### Task 5: Add Tool History Summary Helper

Create a small helper, suggested:

```text
strix/core/tool_history.py
```

The helper should extract compact tool call summaries from SDK sessions:

```python
async def summarise_agent_tool_history(
    coordinator: AgentCoordinator,
    *,
    per_agent_item_limit: int = 400,
    final_tool_limit: int = 300,
) -> dict[str, Any]:
    ...
```

Implementation guidance:

- iterate registered agent ids
- use attached `runtime.session` when present
- call `await session.get_items()`
- slice to the newest `per_agent_item_limit` items before parsing each session
- parse `function_call` items
- join outputs only for status if cheap; do not persist full output
- for `exec_command`, parse command basename and flags from `cmd`
- use `shlex.split()` where possible
- scrub raw command text with existing `strix.core.scrubbing.scrub_secrets`
- length-bound every persisted command summary and error string
- limit stored summaries
- return source health fields: `agents_total`, `agents_with_sessions`, and `extraction_errors`
- scrub and length-bound `extraction_errors`; exception text may carry paths or values

Acceptance criteria:

- handles missing sessions
- handles malformed session items
- extracts shell command basename and flags
- redacts sensitive values
- does not persist full command output
- distinguishes "tool history unavailable" from "tool history available but empty"
- distinguishes fully available history from partially available history
- bounds per-agent session reads before merging
- documents that `session.get_items()` materialises the session first, so the bound limits parsing
  and persisted summaries rather than SDK load cost

### Task 6: Build Review Context

In `review_before_finish`, collect:

- scan mode and target types from `get_global_report_state().scan_config`
- vulnerability count and high-level finding metadata
- agent graph with statuses and metadata
- unresolved todo count and summaries
- note ids/categories/scrubbed bounded tags only; do not persist note previews or raw note titles in
  `qa_review`
- tool history summary
- optional proxy sitemap summary if easily available, storing path only and dropping query strings

Add small helpers if required:

```python
notes_summary(limit: int = 50) -> list[dict[str, Any]]
todo_review_summary() -> dict[str, Any]
```

Keep helpers compact. Do not expose raw note content by default. Rule evaluation may inspect notes
in memory, but persisted review payloads should reference note ids, categories, and scrubbed bounded
tags, and must not echo note free text verbatim.

Target type mapping is explicit:

- web: `web_application`
- IP: `ip_address`
- source: `repository` or `local_code`

Scrub and length-bound every persisted free-text field with `strix.core.scrubbing.scrub_secrets`.
Never persist raw proxy query strings, request/response bodies, headers, cookies, full command
outputs, raw note content, or raw note titles. Treat `scrub_secrets` as best-effort structured-secret
redaction, not general PII detection; avoid persisting unstructured free text wherever possible.

Acceptance criteria:

- review still works when notes/todos/proxy are empty
- optional proxy failures are reported as diagnostics, not exceptions
- context is bounded
- note/proxy-derived secrets are absent from the persisted review
- note previews and raw note titles are absent from the persisted review
- target type mapping uses actual `scan_config` values

### Task 7: Implement Rule Evaluation

Implement simple deterministic rules in `rules.py`.

Recommended pure helpers:

```python
def has_tool(tool_history: list[dict[str, Any]], names: set[str]) -> bool: ...
def has_shell_command(tool_history: list[dict[str, Any]], commands: set[str]) -> bool: ...
def has_signal(text_blobs: list[str], patterns: set[str]) -> bool: ...
def make_gap(...) -> dict[str, Any]: ...
```

`make_gap` must create deterministic `gap_id` values from stable rule and area slugs, for example
`"{rule_key}:{area_key}"`. Do not use counters, timestamps, random suffixes, or list positions.

Minimum MVP rules:

1. `web_application` target without recon/path discovery evidence
2. `ip_address` target without port/service discovery evidence
3. `repository` or `local_code` target without source triage evidence
4. source or version/package signal without CVE/dependency evidence
5. GraphQL signal without GraphQL testing evidence
6. JWT/auth token signal without JWT/auth-session testing evidence
7. upload/file signal without upload/file-handling testing evidence
8. admin/user/id/tenant signal without access-control/IDOR evidence
9. selected tool option gaps for `nmap`, `nuclei`, and `ffuf`

Acceptance criteria:

- if tool history is unavailable, absence-based rules emit one low diagnostic rather than high gaps
- if tool history is partially available with non-empty `extraction_errors`, absence-based gaps are
  downgraded to medium and a partial-coverage diagnostic is emitted
- gaps are sorted critical, high, medium, low
- only top `max_priority_gaps` are returned
- `ready_to_finish` is false if unacknowledged high/critical gaps remain
- acknowledged high/critical gaps move to `deferred_or_residual` and do not block
- `gap_id` values are stable across repeated evaluation of the same context
- medium/low gaps are included as deferred/residual unless they are promoted by clear exposure
- all tool-option gaps are medium and non-blocking by default

### Task 8: Persist Review Result

`review_before_finish` should:

- create a review id
- calculate review metrics with a shared cheap helper
- evaluate rules
- union new `acknowledged_gaps` with any previously persisted acknowledgements
- apply the cumulative acknowledgements to the newly evaluated gaps
- build JSON output
- persist the output via `ReportState.record_qa_review`
- return the same JSON string to the root agent

Acceptance criteria:

- returned and persisted review payloads match
- payload contains no raw secret values from test fixtures
- review result is concise enough for the root agent to act on
- acknowledged high/critical gaps land in `deferred_or_residual`
- acknowledged gaps are not also left in `priority_gaps`
- previously acknowledged gaps stay acknowledged on later reviews

### Task 9: Enforce Gate In finish_scan

Update `strix/tools/finish/tool.py`.

Add a helper:

```python
def _qa_review_blocker(inner: dict[str, Any]) -> dict[str, Any] | None:
    ...
```

Rules:

- if `qa_loop_enabled` is false, no blocker
- if no global report state, do not block solely on QA review because persistence is already degraded
- if latest review missing, block
- if latest review stale according to the shared cheap metrics helper, block
- if `ready_to_finish` is false, block with priority gaps
- if ready, allow existing finish behaviour

Do not remove existing blockers for unresolved agents or todos.
Do not recompute tool history in `finish_scan`.

Acceptance criteria:

- deep scan without review cannot finish
- deep scan with not-ready review cannot finish
- deep scan with ready fresh review can finish
- standard/quick scan can finish without review
- acknowledged high/critical gaps can allow finish once the latest review records them as residual

### Task 10: Prompt Guidance

Update `strix/skills/coordination/root_agent.md` with concise instructions:

- call `review_before_finish` before `finish_scan`
- treat high/critical gaps as follow-up work
- spawn at most three follow-up agents from one review by default
- call `review_before_finish` again after follow-up
- call `review_before_finish(acknowledged_gaps=[...])` only when a high/critical gap has been
  validated through other evidence, is out of scope, or is explicitly accepted as residual risk
- include residual risk in final report if relevant

Do not add long maximalist prompt text.

Acceptance criteria:

- prompt tells root what to do
- finish gate still enforces behaviour if root forgets

### Task 11: Optional TUI Renderer

Optional but useful:

Add a simple renderer for `review_before_finish` under `strix/interface/tui/renderers/`.

Acceptance criteria if implemented:

- shows ready/not ready
- lists top gaps
- does not render raw secrets

This is optional for MVP and should not block backend tests.

## Test Plan

Add tests before or with implementation. Prefer pure and mocked tests.

### New Test File: `tests/test_qa_loop_review.py`

1. `test_review_before_finish_rejects_subagent`
   - Build fake context with `parent_id != None`.
   - Assert JSON has `success: false`.

2. `test_review_persists_latest_review_to_run_record`
   - Create `ReportState`.
   - Call `record_qa_review`.
   - Assert `run_record["qa_review"]` exists and survives `save_run_data()`.

3. `test_tool_history_extracts_exec_command_name_and_options`
   - Fake session items with `function_call` for `exec_command`.
   - Command: `nmap -sV -sC XXXX`.
   - Assert command basename is `nmap` and flags include `-sV`, `-sC`.

4. `test_tool_history_redacts_sensitive_command_values`
   - Fake command includes token/password-like values.
   - Assert raw values are absent and `XXXX` appears.

5. `test_web_target_without_recon_gets_high_gap`
   - Review context has `web_application` target and available tool history but no crawler/path
     discovery tools.
   - Assert high gap about recon/path discovery.

6. `test_web_target_with_katana_or_ffuf_has_no_baseline_recon_gap`
   - Add tool history with `katana` or `ffuf`.
   - Assert baseline recon gap is absent.

7. `test_source_target_without_source_triage_gets_high_gap`
   - `repository` or `local_code` target, no semgrep/sg/trivy/secrets tools.
   - Assert high source triage gap.

8. `test_source_target_with_semgrep_and_trivy_satisfies_source_triage`
   - Add tool history with `semgrep` and `trivy fs`.
   - Assert source triage gap is absent.

9. `test_graphql_signal_without_graphql_testing_gets_gap`
   - Note/path contains `/graphql`.
   - Tool history lacks GraphQL checks.
   - Assert high GraphQL gap.

10. `test_jwt_signal_without_jwt_testing_gets_gap`
    - Text signal includes `jwt` or bearer token wording.
    - Tool history lacks JWT checks.
    - Assert high JWT gap.

11. `test_upload_signal_without_upload_testing_gets_gap`
    - Text signal includes upload/avatar/attachment.
    - Tool history lacks upload checks.
    - Assert high or medium upload gap.

12. `test_admin_user_signal_without_access_control_testing_gets_gap`
    - Text signal includes admin/users/tenant/id.
    - Tool history lacks IDOR/access-control evidence.
    - Assert high access-control gap.

13. `test_nmap_without_service_detection_flags_option_gap_for_ip_target`
   - IP target with `nmap` but no `-sV`/service flag.
   - Assert medium non-blocking option gap.

14. `test_nuclei_default_run_with_known_technology_flags_medium_gap`
    - Technology signal exists.
    - Tool history has `nuclei` with no `-t`, `-tags`, or technology option.
    - Assert medium option gap.

15. `test_ready_true_when_no_high_or_critical_gaps`
    - Build context satisfying minimum rules.
    - Assert review is ready.

16. `test_priority_gaps_are_capped`
   - Create context triggering many gaps.
   - Assert only `max_priority_gaps` are returned.

17. `test_acknowledged_high_gap_allows_ready_and_lands_in_residual`
   - Trigger a high gap.
   - Re-run review with that `gap_id` in `acknowledged_gaps`.
   - Assert `ready_to_finish` is true and the gap is present in `deferred_or_residual`.

18. `test_gap_id_is_stable_across_re_evaluation`
   - Evaluate the same context twice.
   - Assert the same underlying gaps have identical `gap_id` values.
   - Resolve a different gap and acknowledge one id; assert the intended gap is residualised.

19. `test_acknowledged_gaps_persist_across_subsequent_reviews`
   - Acknowledge gap X.
   - Trigger a metric/context change that causes another review.
   - Re-run review without passing X again.
   - Assert X remains in `deferred_or_residual` and does not re-block.

20. `test_tool_history_unavailable_does_not_fire_false_recon_gaps`
   - Review context has targets but `agents_with_sessions == 0`.
   - Assert no high recon/source/CVE absence gaps are emitted and one low diagnostic appears.

21. `test_partial_tool_history_failure_downgrades_absence_gaps`
   - Review context has `agents_with_sessions > 0` and non-empty `extraction_errors`.
   - Trigger an absence-based recon/source gap.
   - Assert the gap is medium and non-blocking, with a partial-coverage diagnostic.

22. `test_review_does_not_persist_note_or_query_secrets`
   - Fixture note and proxy path contain sensitive-looking query values.
   - Assert raw values are absent, `XXXX` appears where relevant, proxy samples contain no query
     string, and no `content_preview` or raw note title is persisted.

23. `test_target_type_mapping_repository_counts_as_source`
   - Use actual target type `repository`.
   - Assert source triage rules apply.

24. `test_tool_history_bounds_per_agent_before_merge`
   - Fake a session with many old items and one newest item.
   - Assert only bounded newest items are parsed before final merge.

25. `test_review_continues_when_proxy_unavailable`
   - Fake optional proxy/Caido summary failure.
   - Assert review still succeeds with a low diagnostic and no exception.

26. `test_resumed_ready_review_with_matching_metrics_allows_finish`
   - Hydrate a run with ready `qa_review` and matching current metrics.
   - Assert deep completion can proceed after existing blockers are clear.

27. `test_resumed_ready_review_with_changed_metrics_reblocks`
   - Hydrate a run with ready `qa_review`.
   - Change current cheap metrics.
   - Assert deep completion blocks as stale.

### Existing Or New Test File: `tests/test_finish_scan_guards.py`

28. `test_deep_completion_blocks_without_qa_review`
    - Root context has `qa_loop_enabled=True`.
    - Existing blockers empty.
    - Assert `finish_scan` returns `scan_completed: false` and required tool.

29. `test_deep_completion_blocks_not_ready_qa_review`
    - Persist review with `ready_to_finish=False`.
    - Assert `finish_scan` blocks and returns priority gaps.

30. `test_deep_completion_allows_ready_fresh_qa_review`
    - Persist review with `ready_to_finish=True` and matching metrics.
    - Assert `finish_scan` succeeds.

31. `test_standard_completion_does_not_require_qa_review`
    - Context has `qa_loop_enabled=False`.
    - Assert existing completion path still works.

32. `test_stale_qa_review_blocks_finish_after_new_vulnerability`
    - Persist ready review with vulnerability_count 0.
    - Add vulnerability to report state or mock current metric count 1.
    - Assert finish blocks as stale.

33. `test_review_metrics_identical_between_tool_and_finish_gate`
    - Use the shared helper from both review creation and finish gating.
    - Assert the same current metrics are produced.

34. `test_existing_unresolved_agent_blockers_still_win`
    - Have unresolved child agent and ready QA review.
    - Assert finish still blocks on unresolved agent.

### Factory Tests

35. `test_root_agent_includes_review_before_finish_tool`
    - Prefer factoring root/child tool selection into a tiny pure helper and testing that helper.
    - Assert tool name exists in root tools.

36. `test_child_agent_does_not_include_review_before_finish_tool`
    - Use the same pure helper.
    - Assert tool name is absent from child tools.

37. `test_child_finish_is_not_qa_gated`
    - Ensure `agent_finish` remains unaffected by `qa_loop_enabled` in inherited context.

### Prompt/Documentation Tests

38. `test_root_agent_skill_mentions_review_before_finish`
    - Read `strix/skills/coordination/root_agent.md`.
    - Assert it mentions `review_before_finish` and `finish_scan`.

## Manual Verification

Run:

```bash
uv run pytest tests/test_qa_loop_review.py tests/test_finish_scan_guards.py
```

Then:

```bash
uv run pytest
make lint
make type-check
```

Manual smoke test:

```bash
uv run strix -n --target https://XXXX.example --scan-mode deep --max-budget-usd 1
```

Expected behaviour:

- root cannot call `finish_scan` before `review_before_finish`
- `review_before_finish` returns a compact review
- high gaps lead to follow-up work or documented residual risk
- final `run.json` contains `qa_review`

Use only authorised test targets. Replace any identifiers, credentials, domains, or client names
with `XXXX` in examples and logs.
