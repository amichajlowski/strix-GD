# Child Agent Error Propagation

## Problem

Child agent failures do not always reach the parent. `_notify_parent_on_crash()` only sends a
message for `crashed`, not `failed` or error-driven `stopped`. Detached child tasks also only catch
budget stop explicitly.

## Current Code Alignment

- `run_agent_loop()` parks interactive child agents on failures.
- `_notify_parent_on_crash()` ignores all statuses except `crashed`.
- `wait_for_message()` can leave a parent waiting until timeout if a child fails without a message.
- `agent_finish()` already sends normal completion reports to parents.

## Functional Scope

Make every terminal child error visible to the parent while preserving the current successful
completion path.

## Non-Goals

- Do not make child failures stop the whole audit.
- Do not force automatic retry loops for every child failure.
- Do not change `agent_finish()` success semantics.

## Ordered Tasks

1. Rename `_notify_parent_on_crash()` to `_notify_parent_on_terminal_error()`.
2. Notify the parent for `failed`, `crashed`, and error-driven `stopped`.
3. Include child id, name, status, redacted error message, and a short suggested parent action.
4. Do not notify for normal `completed`.
5. Do not notify for deliberate user stop unless the stop has an attached error record.
6. In `_start_child_runner()`, catch unexpected child loop exceptions, record structured error,
   mark the child `crashed`, notify the parent, and swallow the exception after logging.
7. Ensure parent wake-up uses existing `coordinator.send()` so it works with current pending message
   counts.

## Test Cases

1. `tests/test_execution.py::test_crashed_child_notifies_parent`
   - Trigger child `crashed`.
   - Assert parent pending count increases and message type is terminal error.
2. `tests/test_execution.py::test_failed_child_notifies_parent`
   - Trigger child `failed`.
   - Assert parent receives child id, status, and redacted error summary.
3. `tests/test_execution.py::test_error_stopped_child_notifies_parent`
   - Mark a child `stopped` with `last_error`.
   - Assert parent is notified.
4. `tests/test_execution.py::test_user_stopped_child_does_not_emit_error_message`
   - Stop a child through the normal graceful stop path without `last_error`.
   - Assert no terminal error message is sent.
5. `tests/test_execution.py::test_child_loop_exception_is_caught_and_recorded`
   - Mock `run_agent_loop()` inside `_start_child_runner()` to raise.
   - Assert child status is `crashed`, metadata contains `last_error`, and no unhandled task
     exception leaks.
6. `tests/test_execution.py::test_budget_stop_does_not_emit_child_error_notification`
   - Raise `BudgetExceededError` from child loop.
   - Assert budget handling remains clean and parent does not receive a misleading failure message.
7. `tests/test_agents_graph.py::test_parent_wait_unblocks_on_child_failure_message`
   - Parent waits for child.
   - Trigger child terminal error notification.
   - Assert `wait_for_message()` returns without waiting for timeout.

## Regression Checks

- Normal child `agent_finish()` still posts one completion report.
- Crashed child still wakes the parent.
- Failed child now wakes the parent.
- Parent waiting on a failed child does not wait for the full timeout.
- Root audit continues after child failures.
- Budget stop remains a clean scan-wide stop and does not create misleading child failure messages.
