# QA Loop Specifications

This folder specifies a lightweight "QA loop" for Strix audit completion.

The feature exists because deep scans can still finish with missed application paths, attack
vectors, CVE checks, tool options, or incomplete follow-up from agent findings. The goal is not to
build a large workflow engine. The goal is a small, practical pre-finish review that makes the root
agent pause, inspect current evidence, identify material gaps, run only worthwhile follow-up work,
and then finish with clear residual risk.

## User Need

Security teams use Strix for authorised audits of applications, APIs, repositories, and deployed
targets. For high-assurance assessments, a first pass is often not enough:

- recon may discover paths that were never tested
- source review may identify risky flows that were not dynamically validated
- CVE or dependency checks may be skipped despite detected frameworks or packages
- tools may run with shallow/default options where target-specific options were needed
- child agents may complete with "nothing found" while leaving obvious follow-up work
- final reports may omit residual areas that were not tested

The QA loop adds a review checkpoint before completion so these gaps are visible and actionable.

## Design Principles

- Keep it simple and functional.
- Add one root-only tool: `review_before_finish`.
- Reuse existing artefacts: reports, todos, notes, agent graph, SDK sessions, and proxy data where
  available.
- Do not create a separate persistence service.
- Do not create a full coverage database for the MVP.
- Do not use an internal LLM call inside the review tool.
- Do not create an infinite loop.
- Persist only compact review results in `run.json`.
- Store no raw secrets, cookies, tokens, request bodies, or client identifiers in the review result.
- Keep quick and standard scans fast; enforce the QA gate by default only for deep scans unless a
  later CLI flag explicitly expands this.

## Specification Order

1. `01-architecture.md`
2. `02-implementation-plan-and-tests.md`
3. `03-handover-prompt.md`
4. `04-feedback-agent-prompt.md`

Run the feedback prompt before implementation, then implement the MVP from the architecture and
plan. The handover prompt is written for a less capable model and should be used as the
implementation brief.
