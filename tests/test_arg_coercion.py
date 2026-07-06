"""Regression tests for stringified-JSON tool-arg coercion.

Weaker function-calling models (e.g. self-hosted vLLM Qwen) emit array/object
tool arguments as JSON *strings* — ``"ports": "[3000]"`` instead of
``"ports": [3000]`` — which fails pydantic list/dict validation and, for
``create_agent``, silently breaks delegation. ``_coerce_stringified_json_args``
decodes those before validation. See ``strix.agents.factory``.
"""

import json

from agents.tool import FunctionTool

from strix.agents import factory
from strix.tools.agents_graph.tools import create_agent
from strix.tools.target_profile.tools import set_target_profile


def _coerce(tool: FunctionTool, args: dict) -> dict:
    return json.loads(
        factory._coerce_stringified_json_args(tool.params_json_schema, json.dumps(args))
    )


def test_stringified_array_is_decoded() -> None:
    out = _coerce(set_target_profile, {"target": "h:3000", "ports": "[3000]", "tech_stack": "[]"})
    assert out["ports"] == [3000]
    assert out["tech_stack"] == []


def test_create_agent_skills_list_decoded() -> None:
    out = _coerce(create_agent, {"name": "X", "task": "t", "skills": '["xss", "sqli"]'})
    assert out["skills"] == ["xss", "sqli"]


def test_string_fields_never_coerced() -> None:
    # A genuine string that happens to start with '[' must survive untouched.
    out = _coerce(set_target_profile, {"target": "h", "notes": "[not] a list"})
    assert out["notes"] == "[not] a list"


def test_already_valid_args_passthrough() -> None:
    args = {"target": "h", "ports": [80, 443]}
    assert _coerce(set_target_profile, args)["ports"] == [80, 443]


def test_null_string_becomes_none_for_nullable_field() -> None:
    # Qwen emits the JSON null keyword as a string for "not provided".
    out = _coerce(set_target_profile, {"target": "h", "waf": "null", "auth_model": "None"})
    assert out["waf"] is None
    assert out["auth_model"] is None


def test_none_lowercase_is_a_valid_value_not_coerced() -> None:
    # "none" is a documented waf/auth value — must survive.
    out = _coerce(set_target_profile, {"target": "h", "waf": "none"})
    assert out["waf"] == "none"


def test_wrap_is_idempotent() -> None:
    first = factory._wrap_arg_coercion(set_target_profile)
    second = factory._wrap_arg_coercion(set_target_profile)
    assert first is second
    assert first._strix_arg_coercion is True
