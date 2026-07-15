"""Build SandboxAgents for root + child Strix runs."""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from agents.agent import ToolsToFinalOutputResult
from agents.sandbox import SandboxAgent
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.errors import InvalidManifestPathError
from agents.tool import CustomTool, FunctionTool, Tool
from pydantic import ValidationError

from strix.agents.prompt import render_system_prompt
from strix.tools.agents_graph.tools import (
    agent_finish,
    create_agent,
    send_message_to_agent,
    stop_agent,
    view_agent_graph,
    wait_for_message,
)
from strix.tools.audit_state.tools import (
    get_audit_state,
    update_audit_state,
)
from strix.tools.finish.tool import finish_scan
from strix.tools.load_skill.tool import load_skill
from strix.tools.loot.tools import (
    delete_loot,
    get_loot,
    record_loot,
)
from strix.tools.notes.tools import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
)
from strix.tools.proxy.tools import (
    list_requests,
    list_sitemap,
    repeat_request,
    scope_rules,
    traffic_health,
    view_request,
    view_sitemap_entry,
)
from strix.tools.qa_loop.tool import review_before_finish
from strix.tools.reporting.tool import create_dependency_report, create_vulnerability_report
from strix.tools.target_profile.tools import (
    get_target_profile,
    set_target_profile,
)
from strix.tools.thinking.tool import think
from strix.tools.todo.tools import (
    create_todo,
    delete_todo,
    list_todos,
    mark_todo_done,
    mark_todo_pending,
    update_todo,
)
from strix.tools.web_search.tool import web_search


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from agents import RunContextWrapper
    from agents.tool import FunctionToolResult


logger = logging.getLogger(__name__)


_CUSTOM_TOOL_INPUT_FIELD_BY_NAME = {
    "apply_patch": "patch",
}
_DEFAULT_CUSTOM_TOOL_INPUT_FIELD = "input"


def _custom_tool_input_field(tool: CustomTool) -> str:
    return _CUSTOM_TOOL_INPUT_FIELD_BY_NAME.get(tool.name, _DEFAULT_CUSTOM_TOOL_INPUT_FIELD)


def _raw_input_schema(tool: CustomTool) -> dict[str, Any]:
    input_field = _custom_tool_input_field(tool)
    return {
        "type": "object",
        "properties": {
            input_field: {
                "type": "string",
                "description": (
                    f"Complete `{tool.name}` payload. Follow the tool description exactly."
                ),
            },
        },
        "required": [input_field],
        "additionalProperties": False,
    }


def _extract_custom_input(tool: CustomTool, raw_input: str | dict[str, Any]) -> str:
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            return ""
    else:
        parsed = raw_input
    value = parsed.get(_custom_tool_input_field(tool))
    return value if isinstance(value, str) else ""


def _format_tool_error(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


def _hallucinated_tool_alias(bad_name: str, real_names: list[str]) -> FunctionTool:
    """Catch a weak model calling a shorthand tool name that doesn't exist.

    The SDK resolves tool calls by exact name before they ever reach our
    ``on_invoke_tool`` wrappers; an unregistered name raises ``ModelBehaviorError``
    and kills the whole agent (see ``strix/tools/notes/tools.py``'s ``create_note``
    docstring, which used to say "use ``todo`` instead" instead of the real name
    ``create_todo`` — exactly the kind of prompt text that primes this mistake).
    Registering the hallucinated name as a real tool that redirects is cheaper
    and safer than teaching ``execution.py`` to retry a ``ModelBehaviorError``:
    nothing in that turn was persisted for the model to learn from on a bare
    retry, so a corrective tool result is the only thing that actually helps.
    """
    suggestion = " or ".join(f"`{n}`" for n in real_names)

    async def invoke(_ctx: Any, _raw_input: str) -> str:
        return f"`{bad_name}` is not a tool. Use {suggestion} instead."

    return FunctionTool(
        name=bad_name,
        description=f"Not a real tool. Use {suggestion} instead.",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": True},
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def _function_tool_with_error_result(tool: FunctionTool) -> FunctionTool:
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            return await invoke_tool(ctx, raw_input)
        except Exception as exc:  # noqa: BLE001 - tool errors should be model-visible results.
            logger.debug("Tool %s failed; returning error as result", tool.name, exc_info=True)
            return _format_tool_error(exc)

    tool.on_invoke_tool = invoke
    return tool


def _custom_tool_as_function_tool(tool: CustomTool) -> FunctionTool:
    async def invoke(ctx: Any, raw_input: str) -> Any:
        custom_input = _extract_custom_input(tool, raw_input)
        if not custom_input:
            return f"`{_custom_tool_input_field(tool)}` must be a non-empty string."
        try:
            return await tool.on_invoke_tool(ctx, custom_input)
        except Exception as exc:  # noqa: BLE001 - matches SDK CustomTool error-as-result behavior.
            logger.debug("Tool %s failed; returning error as result", tool.name, exc_info=True)
            return _format_tool_error(exc)

    needs_approval = tool.runtime_needs_approval()
    function_needs_approval: bool | Callable[[Any, dict[str, Any], str], Awaitable[bool]]
    if callable(needs_approval):

        async def approve(ctx: Any, args: dict[str, Any], call_id: str) -> bool:
            result = needs_approval(ctx, _extract_custom_input(tool, args), call_id)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)

        function_needs_approval = approve
    else:
        function_needs_approval = needs_approval

    return FunctionTool(
        name=tool.name,
        description=(
            f"{tool.description}\n\n"
            f"Pass the complete `{tool.name}` payload in `{_custom_tool_input_field(tool)}`."
        ),
        params_json_schema=_raw_input_schema(tool),
        on_invoke_tool=invoke,
        strict_json_schema=False,
        needs_approval=function_needs_approval,
    )


def _configure_chat_completions_filesystem_tools(toolset: Any) -> None:
    for name, tool in vars(toolset).items():
        if isinstance(tool, CustomTool):
            setattr(toolset, name, _custom_tool_as_function_tool(tool))
        elif isinstance(tool, FunctionTool):
            setattr(toolset, name, _function_tool_with_error_result(tool))


_CHARS_ESCAPE_RE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[0abtnvfr\\])")
_CHARS_ESCAPE_MAP = {
    "\\\\": "\\",
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    "\\0": "\x00",
    "\\a": "\x07",
    "\\b": "\x08",
    "\\v": "\x0b",
    "\\f": "\x0c",
}


def _decode_chars_escape(s: str) -> str:
    if "\\" not in s:
        return s

    def sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in _CHARS_ESCAPE_MAP:
            return _CHARS_ESCAPE_MAP[token]
        if token.startswith(("\\u", "\\x")):
            return chr(int(token[2:], 16))
        return token

    return _CHARS_ESCAPE_RE.sub(sub, s)


def _allowed_json_types(prop_schema: dict[str, Any]) -> set[str]:
    """Collect the JSON types a property accepts, flattening ``anyOf``/``oneOf``."""
    types: set[str] = set()
    t = prop_schema.get("type")
    if isinstance(t, str):
        types.add(t)
    elif isinstance(t, list):
        types.update(x for x in t if isinstance(x, str))
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in prop_schema.get(key, []) or []:
            if isinstance(sub, dict):
                types |= _allowed_json_types(sub)
    return types


def _coerce_stringified_json_args(schema: dict[str, Any], raw_input: str) -> str:
    """Normalize args a weak function-calling model mis-serialized.

    Two fixes, both scoped by the field's JSON schema so real values are safe:

    * ``"ports": "[3000]"`` → ``[3000]`` — arrays/objects sent as JSON strings
      (only fields whose schema accepts array/object and not string).
    * ``"confidence": "null"`` → ``None`` — the JSON ``null`` keyword emitted as a
      string to mean "not provided" (only nullable fields; the exact tokens
      ``null``/``None`` — never ``"none"``, which is a valid waf/auth value).
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return raw_input
    try:
        args = json.loads(raw_input)
    except (json.JSONDecodeError, TypeError):
        return raw_input
    if not isinstance(args, dict):
        return raw_input

    changed = False
    for key, value in list(args.items()):
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        prop = props.get(key)
        kinds = _allowed_json_types(prop) if isinstance(prop, dict) else set()
        if stripped in ("null", "None") and "null" in kinds:
            args[key] = None
            changed = True
            continue
        if not stripped or stripped[0] not in "[{":
            continue
        if "string" in kinds or not ({"array", "object"} & kinds):
            continue
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if (isinstance(decoded, list) and "array" in kinds) or (
            isinstance(decoded, dict) and "object" in kinds
        ):
            args[key] = decoded
            changed = True

    return json.dumps(args) if changed else raw_input


def _wrap_arg_coercion(tool: FunctionTool) -> FunctionTool:
    # _BASE_TOOLS are module singletons reused across every agent spawn; wrap once.
    if getattr(tool, "_strix_arg_coercion", False):
        return tool
    invoke_tool = tool.on_invoke_tool
    schema = tool.params_json_schema

    async def invoke(ctx: Any, raw_input: str) -> Any:
        raw_input = _coerce_stringified_json_args(schema, raw_input)
        try:
            return await invoke_tool(ctx, raw_input)
        except ValidationError as exc:
            return _format_validation_error(tool.name, exc)

    tool.on_invoke_tool = invoke
    tool._strix_arg_coercion = True  # type: ignore[attr-defined]
    return tool


def _format_validation_error(tool_name: str, exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return f"{tool_name}: invalid arguments — " + "; ".join(parts)


def _wrap_exec_command(tool: FunctionTool) -> FunctionTool:
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and "shell" not in parsed:
            parsed["shell"] = "bash"
            raw_input = json.dumps(parsed)
        try:
            return await invoke_tool(ctx, raw_input)
        except ValidationError as exc:
            return _format_validation_error(tool.name, exc)
        except InvalidManifestPathError as exc:
            rel = exc.context.get("rel", "?")
            return (
                "exec_command: workdir must be a path inside /workspace "
                "(or omitted to use the turn's cwd). "
                f"Got: {rel!r}."
            )

    tool.on_invoke_tool = invoke
    return tool


def _wrap_write_stdin(tool: FunctionTool) -> FunctionTool:
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("chars"), str):
            parsed["chars"] = _decode_chars_escape(parsed["chars"])
            raw_input = json.dumps(parsed)
        try:
            return await invoke_tool(ctx, raw_input)
        except ValidationError as exc:
            return _format_validation_error(tool.name, exc)

    tool.on_invoke_tool = invoke
    return tool


def _configure_shell_tools(toolset: Any, *, chat_completions: bool) -> None:
    for name, tool in vars(toolset).items():
        if not isinstance(tool, FunctionTool):
            continue
        wrapped = tool
        if tool.name == "exec_command":
            wrapped = _wrap_exec_command(wrapped)
        elif tool.name == "write_stdin":
            wrapped = _wrap_write_stdin(wrapped)
        if chat_completions:
            wrapped = _function_tool_with_error_result(wrapped)
        setattr(toolset, name, wrapped)


def _make_shell_configurator(*, chat_completions: bool) -> Any:
    def configure(toolset: Any) -> None:
        _configure_shell_tools(toolset, chat_completions=chat_completions)

    return configure


def _lifecycle_tool_completed(tool_name: str, output: Any) -> bool:
    if tool_name == "agent_finish":
        completion_key = "agent_completed"
    elif tool_name == "finish_scan":
        completion_key = "scan_completed"
    else:
        return False

    if not isinstance(output, str):
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    return bool(isinstance(parsed, dict) and parsed.get("success") and parsed.get(completion_key))


def _wait_tool_parked(tool_name: str, output: Any) -> bool:
    if tool_name != "wait_for_message" or not isinstance(output, str):
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(parsed, dict)
        and parsed.get("success")
        and parsed.get("wait_outcome") == "waiting"
    )


def _finish_tool_use_behavior(
    ctx: RunContextWrapper[Any],
    tool_results: list[FunctionToolResult],
) -> ToolsToFinalOutputResult:
    """Stop only after a lifecycle tool reports successful completion."""
    interactive = (
        bool(ctx.context.get("interactive", False)) if isinstance(ctx.context, dict) else False
    )
    for tool_result in tool_results:
        if _lifecycle_tool_completed(tool_result.tool.name, tool_result.output):
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=tool_result.output,
            )
        if interactive and _wait_tool_parked(tool_result.tool.name, tool_result.output):
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=tool_result.output,
            )
    return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


_BASE_TOOLS: tuple[Tool, ...] = (
    think,
    load_skill,
    create_todo,
    list_todos,
    update_todo,
    mark_todo_done,
    mark_todo_pending,
    delete_todo,
    create_note,
    list_notes,
    get_note,
    update_note,
    delete_note,
    web_search,
    create_vulnerability_report,
    create_dependency_report,
    list_requests,
    view_request,
    repeat_request,
    list_sitemap,
    view_sitemap_entry,
    scope_rules,
    traffic_health,
    record_loot,
    get_loot,
    delete_loot,
    set_target_profile,
    get_target_profile,
    get_audit_state,
    update_audit_state,
    view_agent_graph,
    send_message_to_agent,
    wait_for_message,
    create_agent,
    stop_agent,
    _hallucinated_tool_alias("note", ["create_note", "get_note", "update_note", "delete_note"]),
    _hallucinated_tool_alias("todo", ["create_todo", "update_todo", "mark_todo_done"]),
)


# Extra tools registered for scan agents. Mirrors
# ``strix.runtime.backends.register_backend``: register before the first
# ``build_strix_agent`` call and every agent (root + children) gets them.
_EXTRA_TOOLS: list[Tool] = []


def _ensure_unique_tool_names(tools: Sequence[Tool]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            duplicates.add(tool.name)
        seen.add(tool.name)
    if duplicates:
        msg = f"Agent tools must have unique names: {sorted(duplicates)}"
        raise ValueError(msg)


def register_agent_tools(*tools: Tool) -> None:
    """Register tools for every scan agent built afterwards.

    Tools are added to both root and child agents, after the base set and
    before the lifecycle tool (``finish_scan`` / ``agent_finish``). Duplicate
    tool objects are ignored so repeated imports don't double-register.
    """
    new_tools: list[Tool] = []
    for tool in tools:
        if tool not in _EXTRA_TOOLS and tool not in new_tools:
            new_tools.append(tool)

    _ensure_unique_tool_names([*_BASE_TOOLS, *_EXTRA_TOOLS, *new_tools, finish_scan, agent_finish])

    for tool in new_tools:
        _EXTRA_TOOLS.append(tool)
        logger.info("Registered extra agent tool: %s", getattr(tool, "name", tool))


def registered_agent_tools() -> tuple[Tool, ...]:
    """Return the currently registered scan-agent tools."""
    return tuple(_EXTRA_TOOLS)


def select_tools(*, is_root: bool, extra_tools: Sequence[Tool] | None = None) -> list[Tool]:
    """Assemble the tool set for a scan agent and return it ready to attach.

    Base tools + tools registered via ``register_agent_tools`` + this agent's
    ``extra_tools`` + the lifecycle tool. Root agents also get the QA review gate
    (``review_before_finish``) ahead of ``finish_scan``; children get
    ``agent_finish``. Every ``FunctionTool`` is wrapped for arg coercion so weak
    function-calling models can't crash the agent with malformed / stringified
    args. Single source of truth shared by ``build_strix_agent`` and the tests.
    """
    agent_tools = [*_EXTRA_TOOLS, *(extra_tools or [])]
    if is_root:
        # review_before_finish is the root QA gate; keep registered extras
        # immediately before the finish tool, matching register_agent_tools' contract.
        tools: list[Tool] = [*_BASE_TOOLS, review_before_finish, *agent_tools, finish_scan]
    else:
        tools = [*_BASE_TOOLS, *agent_tools, agent_finish]
    _ensure_unique_tool_names(tools)
    return [_wrap_arg_coercion(t) if isinstance(t, FunctionTool) else t for t in tools]


def build_strix_agent(
    *,
    name: str = "strix",
    skills: list[str] | None = None,
    is_root: bool,
    scan_mode: str = "deep",
    is_whitebox: bool = False,
    interactive: bool = False,
    chat_completions_tools: bool = False,
    system_prompt_context: dict[str, Any] | None = None,
    extra_tools: Sequence[Tool] | None = None,
    instructions_override: str | None = None,
) -> SandboxAgent[Any]:
    """Build a SandboxAgent for either root or child use.

    Args:
        chat_completions_tools: Wrap SDK custom tools as function tools
            when the selected backend cannot accept Responses custom tools.
        extra_tools: Additional tools for this scan agent only, on top of any
            registered via ``register_agent_tools``.
        instructions_override: Use this verbatim as the system prompt instead
            of rendering the built-in scan prompt.
    """
    if instructions_override is not None:
        instructions = instructions_override
    else:
        instructions = render_system_prompt(
            skills=skills,
            scan_mode=scan_mode,
            is_whitebox=is_whitebox,
            is_root=is_root,
            interactive=interactive,
            system_prompt_context=system_prompt_context,
        )

    tools = select_tools(is_root=is_root, extra_tools=extra_tools)

    logger.info(
        "Built %s agent '%s' (skills=%d, tools=%d, scan_mode=%s, whitebox=%s)",
        "root" if is_root else "child",
        name,
        len(skills or []),
        len(tools),
        scan_mode,
        is_whitebox,
    )

    return SandboxAgent(
        name=name,
        instructions=instructions,
        tools=tools,
        tool_use_behavior=_finish_tool_use_behavior,
        model=None,
        capabilities=[
            Filesystem(
                configure_tools=(
                    _configure_chat_completions_filesystem_tools if chat_completions_tools else None
                ),
            ),
            Shell(
                configure_tools=_make_shell_configurator(
                    chat_completions=chat_completions_tools,
                ),
            ),
        ],
    )


def make_child_factory(
    *,
    scan_mode: str = "deep",
    is_whitebox: bool = False,
    interactive: bool = False,
    chat_completions_tools: bool = False,
    system_prompt_context: dict[str, Any] | None = None,
) -> Any:
    """Return the runner-owned builder used by ``spawn_child_agent``.

    Run-level arguments (``scan_mode``, ``is_whitebox``, etc.) are
    captured in a closure so each child inherits scan-level configuration
    without the graph tool knowing about runner internals.
    """

    def _factory(*, name: str, skills: list[str]) -> SandboxAgent[Any]:
        return build_strix_agent(
            name=name,
            skills=skills,
            is_root=False,
            scan_mode=scan_mode,
            is_whitebox=is_whitebox,
            interactive=interactive,
            chat_completions_tools=chat_completions_tools,
            system_prompt_context=system_prompt_context,
        )

    return _factory
