# QA Loop Architecture

## Summary

Add a bounded pre-finish review gate to deep Strix scans.

Current deep scan lifecycle:

```text
audit run
root decides complete
finish_scan
```

New lifecycle:

```text
audit run
root calls review_before_finish
review returns high-value gaps or ready=true
root runs focused follow-up work for high/critical gaps
root calls review_before_finish again if needed
finish_scan accepts only after a fresh ready review
```

This is a finish gate, not a new orchestration framework.

## Why This Feature Exists

Deep mode currently tells agents to be exhaustive, but completion is mostly guarded by unresolved
agents, unresolved todos, and filed vulnerability reports. That catches obvious unfinished work, but
it does not ask whether the audit actually covered the target well.

The QA loop addresses practical audit quality questions:

- Did recon find endpoints, roles, APIs, or services that were not tested?
- Did source review find risky code paths that were not validated?
- Were dependency and CVE checks run for detected frameworks or packages?
- Were relevant tools run with appropriate options, templates, wordlists, or modes?
- Did agent completion summaries contain recommendations that were never followed?
- Are remaining gaps documented as residual risk rather than silently ignored?

## Non-Goals

- No general compliance workflow system.
- No per-path coverage database in the MVP.
- No separate storage backend.
- No infinite self-improvement loop.
- No broad taxonomy of every possible security tool and option.
- No automatic vulnerability reporting from the QA review itself.
- No automatic fixing or patching logic beyond the existing white-box agent workflow.
- No raw transcript or raw request/response persistence in `run.json`.

## New Root Tool

Create a root-only tool named:

```text
review_before_finish
```

Suggested location:

```text
strix/tools/qa_loop/tool.py
strix/tools/qa_loop/__init__.py
```

Register it only for root agents in `strix/agents/factory.py`, beside `finish_scan`.

Subagents must not be able to call this tool. If a subagent calls it, return a structured error
similar to `finish_scan`.

## Tool Contract

Tool input should be intentionally small:

```python
async def review_before_finish(
    ctx: RunContextWrapper,
    reason: str = "pre-finish audit quality review",
    max_priority_gaps: int = 5,
) -> str:
    ...
```

Do not ask the agent to pass findings, tool history, or coverage manually. The tool should collect
what it can from existing run state.

The JSON output should be stable and compact:

```json
{
  "success": true,
  "ready_to_finish": false,
  "review_id": "qa_20260625_120000_ab12",
  "created_at": "2026-06-25T12:00:00Z",
  "reason": "pre-finish audit quality review",
  "summary": "Review found high-priority gaps in JWT validation and dependency CVE coverage.",
  "priority_gaps": [
    {
      "gap_id": "gap-001",
      "priority": "high",
      "area": "JWT authentication",
      "reason": "JWT/session handling was observed, but no JWT-specific validation was recorded.",
      "suggested_action": "Run focused JWT validation for algorithm confusion, weak secrets, expiry and claim tampering.",
      "evidence": ["note:auth-token", "tool_history:no-jwt-tool"],
      "suggested_skills": ["authentication_jwt"]
    }
  ],
  "deferred_or_residual": [
    {
      "area": "Technology-specific nuclei templates",
      "reason": "Useful but lower priority after targeted checks passed."
    }
  ],
  "review_metrics": {
    "scan_mode": "deep",
    "vulnerability_count": 2,
    "agent_count": 8,
    "unresolved_todo_count": 0,
    "tool_call_count": 64
  }
}
```

`priority_gaps` should contain only actionable high-value gaps. Cap it with `max_priority_gaps`
after sorting by priority and confidence.

## Persistence

Persist the latest review result under `run.json`:

```json
{
  "qa_review": {
    "review_id": "qa_20260625_120000_ab12",
    "created_at": "2026-06-25T12:00:00Z",
    "ready_to_finish": false,
    "summary": "...",
    "priority_gaps": [],
    "deferred_or_residual": [],
    "review_metrics": {}
  }
}
```

Add methods on `ReportState`:

```python
record_qa_review(review: dict[str, Any]) -> None
get_latest_qa_review() -> dict[str, Any] | None
```

Use `save_run_data()` for persistence. Do not create a new file for the MVP.

## Finish Gate

Update `finish_scan` so deep scans cannot finish unless the latest QA review says
`ready_to_finish: true`.

The gate should be enforced after existing unresolved-agent and unresolved-todo checks, or as part
of the same blocker response.

If no review exists, return:

```json
{
  "success": false,
  "scan_completed": false,
  "error": "QA review required before finishing a deep scan",
  "required_tool": "review_before_finish"
}
```

If the review exists but is not ready, return:

```json
{
  "success": false,
  "scan_completed": false,
  "error": "Cannot finish scan while QA review has high-priority gaps",
  "qa_review": {
    "review_id": "...",
    "priority_gaps": [...]
  }
}
```

If the review is stale, return the same shape with:

```text
error: "QA review is stale; run review_before_finish again"
```

## Stale Review Rules

Keep stale detection cheap. The review is stale if current metrics differ from the persisted
`review_metrics` in any of these fields:

- vulnerability count
- agent count or terminal status digest
- unresolved todo count
- tool call count, if tool history is available

Do not hash full transcripts. Do not compare full vulnerability content. The purpose is to catch
material changes after the review, not to prove perfect immutability.

## Scan Mode Behaviour

MVP behaviour:

- `deep`: QA review is mandatory before `finish_scan`.
- `standard`: tool is available but not mandatory.
- `quick`: tool is available only if simple to expose; not mandatory.

To implement this, pass these fields through runner context:

```python
"scan_mode": scan_mode,
"qa_loop_enabled": scan_mode == "deep",
```

`finish_scan` should enforce the QA gate only when `qa_loop_enabled` is true.

Do not add a CLI flag in the MVP unless implementation is already complete and tests remain small.
A future flag could be `--qa-loop` / `--no-qa-loop` or `--assurance-level`, but that is not required
for this spec.

## Evidence Sources

`review_before_finish` should collect a compact review context from existing sources.

Required sources:

- `ReportState.vulnerability_reports`
- `ReportState.run_record` and scan config fields
- `AgentCoordinator.graph_snapshot_with_metadata()`
- unresolved todos via existing todo storage helpers
- notes summaries via a small helper in `strix/tools/notes/tools.py`
- SDK session tool calls from attached agent sessions where available

Optional sources:

- proxy sitemap summary through existing proxy/Caido context when easy to access
- request count, path samples, and host samples from proxy history

If an optional source is unavailable, the review should continue and add a low-priority diagnostic
warning. Optional source failure must not crash the audit.

## Tool History, Without Overengineering

Do not build a separate tool-run ledger for the MVP.

Instead, summarise tool usage from existing SDK session items. The TUI already parses session
history and stream events for tool calls in `strix/interface/tui/live_view.py`. Reuse or extract the
same idea into a small helper, for example:

```text
strix/core/tool_history.py
```

The helper should return compact entries:

```json
{
  "agent_id": "root1234",
  "tool_name": "exec_command",
  "command": "nuclei",
  "key_options": ["-t", "-tags", "-severity"],
  "status": "completed"
}
```

Rules:

- Parse SDK items with `type == "function_call"` and `type == "function_call_output"`.
- For `exec_command`, parse only command basename and option flags from `cmd`.
- For wrapped filesystem/shell tools, handle both direct and wrapped argument shapes.
- Do not store full command output in `run.json`.
- Scrub all command strings before storing summaries.
- Drop environment variable assignments and values for sensitive-looking flags.
- Limit history to a bounded count, e.g. newest 300 tool calls across agents.

## Review Rules

Keep the rules small and explicit. They should produce prompts for follow-up, not final security
truth.

Suggested rule module:

```text
strix/tools/qa_loop/rules.py
```

Rules should inspect simple signals from notes, reports, paths, targets, and tool history.

### Baseline Recon Rules

For web targets:

- If no path discovery or crawler activity is recorded, add a high gap.
- Evidence of acceptable activity includes `katana`, `ffuf`, `dirsearch`, `gospider`,
  `list_sitemap`, or a meaningful proxy sitemap summary.

For IP targets:

- If no port/service discovery is recorded, add a high gap.
- Evidence includes `nmap`, `naabu`, or equivalent commands.

For source targets:

- If no source triage is recorded, add a high gap.
- Evidence includes `semgrep`, `sg`, Tree-sitter, `gitleaks`, `trufflehog`, `trivy fs`, `bandit`
  where relevant.

### Application Path Rules

If notes, sitemap, reports, or tool output summaries mention obvious application surfaces, require
matching validation evidence:

- `admin`, `users`, `accounts`, `organisations`, `tenants`, numeric ids, UUIDs:
  access-control/IDOR/BFLA validation should be present.
- `upload`, `import`, `avatar`, `document`, `attachment`:
  file upload bypass and file handling validation should be present.
- `webhook`, `callback`, `redirect`, `url`, `next`:
  SSRF/open redirect/signature validation should be considered.
- `graphql`:
  GraphQL-specific checks should be present.
- `jwt`, `token`, `session`, `oauth`, `sso`:
  auth/session/JWT checks should be present.

### CVE And Dependency Rules

If the scan has a source target or technology/version signals:

- require at least one dependency/CVE check where relevant
- evidence includes `trivy`, `npm audit`, `pip-audit`, `retire`, `vulnx`, `cvemap`,
  package-manager audit commands, or targeted web search

If a framework/version is detected but no CVE check is recorded, add a medium or high gap depending
on exposure:

- high if the component appears externally exposed or tied to auth/file upload/admin paths
- medium otherwise

### Tool Option Rules

Only flag missing options when the target clearly warrants them. Avoid nitpicking.

Examples:

- `nuclei` ran with no technology-specific templates/tags despite known technology signals:
  medium gap.
- `ffuf` ran without extensions on a target that appears to serve files:
  medium gap.
- `nmap` ran without service/version detection against an IP/service target:
  high gap.
- `sqlmap` ran once against a broad target without focused parameter evidence:
  medium gap.

Do not block finish for every possible option. Raise only gaps that would likely change audit
quality.

## Root Agent Behaviour

Update `strix/skills/coordination/root_agent.md` or the main system prompt with short guidance:

- Before `finish_scan`, call `review_before_finish`.
- If high/critical gaps are returned, create focused todos or agents for only the top gaps.
- Do not spawn more than three follow-up workstreams from one review unless the target is clearly
large and active budget remains.
- After follow-up work completes, call `review_before_finish` again.
- If the review says ready, call `finish_scan`.
- Include residual/deferred areas in the final methodology or recommendations where relevant.

Keep prompt changes concise. The `finish_scan` guard is the real enforcement.

## Example Use Cases

### Use Case 1: JWT Seen But Not Tested

Recon and notes mention bearer tokens. Tool history has browser/proxy work, but no `jwt_tool`, no
JWT skill, and no command or note showing algorithm confusion, weak secret, expiry, or claim testing.

QA review returns:

```json
{
  "priority": "high",
  "area": "JWT authentication",
  "suggested_action": "Run focused JWT validation for algorithm confusion, weak secrets, expiry and claim tampering.",
  "suggested_skills": ["authentication_jwt"]
}
```

The root creates one focused auth/JWT validation agent.

### Use Case 2: GraphQL Discovered Late

Sitemap includes `/graphql`. No introspection, batching, depth, alias, or authorisation checks are
recorded.

QA review returns a high-priority GraphQL gap and suggests a focused GraphQL agent.

### Use Case 3: Source Target Without CVE Coverage

The repository contains `package.json` and detected framework versions. Tool history shows source
reading and semgrep, but no `trivy`, `npm audit`, `retire`, or targeted CVE lookup.

QA review returns a high or medium gap depending on exposure.

### Use Case 4: Deep Scan Ready

Findings are reported, agents are terminal, todos are resolved, recon/path discovery exists, source
triage exists for source targets, and no high-priority review rules trigger.

QA review stores:

```json
{
  "ready_to_finish": true,
  "priority_gaps": [],
  "deferred_or_residual": []
}
```

`finish_scan` can complete.

