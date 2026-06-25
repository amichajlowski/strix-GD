# QA Loop Handover Prompt

Use this prompt with the slash loop command for implementation.

```text
/loop

Objective:
Implement the Strix QA loop pre-finish review gate exactly as specified in
Specs/qa-loop/README.md, Specs/qa-loop/01-architecture.md, and
Specs/qa-loop/02-implementation-plan-and-tests.md.

Business reason:
Deep Strix audits can currently finish even when important application paths, attack vectors, CVE
checks, or relevant tool options were missed. The feature adds a lightweight quality review before
completion so high-value gaps are run or documented before the final report.

Implementation style:
Keep it functional, small, and not overengineered. Add one root-only tool named
review_before_finish. Reuse existing report state, agent coordinator state, todos, notes, SDK
sessions, and proxy data where available. Do not create a workflow engine, a separate persistence
service, or a large coverage database.

Primary files to modify or create:
- strix/tools/qa_loop/__init__.py
- strix/tools/qa_loop/tool.py
- strix/tools/qa_loop/rules.py
- strix/core/tool_history.py
- strix/report/state.py
- strix/core/runner.py
- strix/agents/factory.py
- strix/tools/finish/tool.py
- strix/skills/coordination/root_agent.md
- tests/test_qa_loop_review.py
- tests/test_finish_scan_guards.py

Required behaviour:
1. Root agents can call review_before_finish.
2. Subagents cannot call review_before_finish.
3. review_before_finish collects bounded review context from existing artefacts.
4. review_before_finish evaluates simple deterministic rules for missed recon, app paths, CVE checks,
   and tool option gaps.
5. review_before_finish persists the latest result in run.json under qa_review.
6. Deep scans cannot finish unless qa_review.ready_to_finish is true and fresh.
7. Quick and standard scans must not be blocked by missing qa_review.
8. Existing finish_scan blockers for unresolved agents and todos must keep working.
9. The review must not persist raw secrets, cookies, tokens, request bodies, full command outputs, or
   client identifiers.
10. The gate must never deadlock: acknowledged/residual high gaps must allow finishing after they
    are recorded by review_before_finish.
11. Prompt guidance must tell the root agent to call review_before_finish before finish_scan and run
    only high-value follow-up gaps.

Required gap id and acknowledgement behaviour:
- Derive each gap_id deterministically from rule + area, for example "{rule_key}:{area_key}".
- Never use counters, timestamps, random suffixes, list positions, or generated ids for gap_id.
- review_before_finish must union newly acknowledged ids with any previously persisted
  qa_review.acknowledged_gaps before filtering gaps.
- Acknowledged gaps must move to deferred_or_residual and must not remain in priority_gaps.

Required privacy handling:
- Scrub and length-bound every persisted free-text field with strix.core.scrubbing.scrub_secrets.
- For proxy samples store path only. Never persist req.query. Never call view_request for QA review
  context.
- For the MVP do not persist note previews, raw note content, or raw note titles. Persist note ids,
  categories, and scrubbed bounded tags only. Rule evaluation may inspect notes in memory, but
  qa_review must not echo note free text verbatim.

Required target mapping:
- web = scan_config target type web_application
- IP = scan_config target type ip_address
- source = scan_config target type repository or local_code
- Read targets from get_global_report_state().scan_config, not from agent context.

Required tool-history semantics:
- Distinguish "tool history unavailable" from "tool history available but empty".
- If agents_with_sessions == 0 or extraction fails for all sessions, absence-based rules must emit
  one low diagnostic instead of high recon/source/CVE gaps.
- If extraction_errors is non-empty but some sessions were read, absence-based gaps must be medium
  and non-blocking, with a partial-history diagnostic.
- Bound history per agent before merging: inspect only newest N session items per agent, then merge.
- await session.get_items() materialises the full SDK session before local slicing. That is acceptable
  only inside review_before_finish; never do this inside finish_scan.

Rule MVP:
- web_application target without path discovery/crawler evidence -> high gap if tool history is available
- ip_address target without port/service discovery evidence -> high gap if tool history is available
- repository/local_code target without source triage evidence -> high gap if tool history is available
- source/package/version signal without dependency/CVE evidence -> medium/high gap
- GraphQL signal without GraphQL testing evidence -> high gap
- JWT/auth token signal without JWT/auth testing evidence -> high gap
- upload/file signal without upload/file handling evidence -> high/medium gap
- admin/user/id/tenant signal without access-control/IDOR evidence -> high gap
- nmap without service/version detection on IP target -> medium option gap
- nuclei default run with known technology signal -> medium option gap
- ffuf without useful path/file options when file-like paths are in scope -> medium option gap

Testing objectives:
Implement the tests listed in Specs/qa-loop/02-implementation-plan-and-tests.md. At minimum, include
unit tests for rule evaluation, tool history summarisation, review persistence, finish_scan gating,
and root-only tool registration.

Suggested implementation sequence:
1. Add tests for pure rule evaluation and review persistence.
2. Implement ReportState qa_review methods.
3. Implement tool_history summariser with safe redaction and bounded output.
4. Implement qa_loop rules.
5. Implement review_before_finish tool and persistence.
6. Register the tool for root agents only.
7. Add scan_mode and qa_loop_enabled to the root context dict in runner.py. Children inherit it
   automatically through dict(parent_ctx); do not edit execution.py solely for this.
8. Enforce the finish_scan gate for deep scans.
9. Update root-agent prompt guidance.
10. Run the targeted tests, then the full suite if feasible.

Resume/reconfiguration behaviour:
- Do not clear qa_review in set_scan_config just because a scan is resumed.
- Let the shared cheap stale-metrics helper decide whether the persisted qa_review is still valid.
- Add tests for matching metrics allowing finish and changed metrics re-blocking.

Stop conditions:
- Do not proceed if implementation requires a new persistence service.
- Do not add broad CLI changes unless all backend behaviour is already passing.
- Do not store raw sensitive values in qa_review.
- Do not weaken existing finish_scan unresolved-agent or unresolved-todo guards.
- Do not change quick/standard scan completion semantics.
- Do not recompute tool history inside finish_scan. Use only cheap shared metrics for stale checks:
  vulnerability_count, agent_count, unresolved_todo_count.

Verification commands:
uv run pytest tests/test_qa_loop_review.py tests/test_finish_scan_guards.py
uv run pytest
make lint
make type-check

Final response requirements:
Summarise changed files, tests run, and any skipped tests. Note any residual risk or TODOs clearly.
Use British English and anonymise personal or client identifiers as XXXX.
```
