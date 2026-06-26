"""Pure, deterministic QA-review rules.

No live LLM, network, Docker, or Caido dependency — rules consume a plain
``review_context`` dict and emit gap dicts. Gap ids are derived only from
stable ``{rule_key}:{area_key}`` slugs so acknowledgements survive across
review calls.
"""

from __future__ import annotations

from typing import Any


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_BLOCKING_PRIORITIES = {"critical", "high"}

# Evidence vocabularies (tool names or shell-command basenames).
_RECON_WEB = {"katana", "ffuf", "dirsearch", "gospider", "hakrawler", "list_sitemap", "feroxbuster"}
_RECON_IP = {"nmap", "naabu", "masscan", "rustscan"}
_SOURCE_TRIAGE = {"semgrep", "sg", "gitleaks", "trufflehog", "trivy", "bandit"}
_CVE_TOOLS = {"trivy", "npm", "pip-audit", "retire", "vulnx", "cvemap", "osv-scanner", "safety"}


def make_gap(
    *,
    rule_key: str,
    area_key: str,
    priority: str,
    area: str,
    reason: str,
    suggested_action: str,
    evidence: list[str] | None = None,
    suggested_skills: list[str] | None = None,
) -> dict[str, Any]:
    """Build a gap with a deterministic ``gap_id`` (``{rule_key}:{area_key}``)."""
    return {
        "gap_id": f"{rule_key}:{area_key}",
        "priority": priority,
        "area": area,
        "reason": reason,
        "suggested_action": suggested_action,
        "evidence": evidence or [],
        "suggested_skills": suggested_skills or [],
    }


def _combined(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("tool_name") or ""),
        str(entry.get("command") or ""),
        " ".join(str(o) for o in entry.get("key_options") or []),
    ]
    return " ".join(parts).lower()


def has_tool(tool_history: list[dict[str, Any]], names: set[str]) -> bool:
    """True if any entry's tool name or shell-command basename is in ``names``."""
    lowered = {n.lower() for n in names}
    for entry in tool_history:
        if str(entry.get("tool_name") or "").lower() in lowered:
            return True
        if str(entry.get("command") or "").lower() in lowered:
            return True
    return False


def has_shell_command(tool_history: list[dict[str, Any]], commands: set[str]) -> bool:
    lowered = {c.lower() for c in commands}
    return any(str(e.get("command") or "").lower() in lowered for e in tool_history)


def has_signal(text_blobs: list[str], patterns: set[str]) -> bool:
    lowered = [b.lower() for b in text_blobs if b]
    return any(p.lower() in blob for p in patterns for blob in lowered)


def _has_evidence_keyword(tool_history: list[dict[str, Any]], keywords: set[str]) -> bool:
    lowered = {k.lower() for k in keywords}
    return any(any(k in _combined(e) for k in lowered) for e in tool_history)


def _absence_priority(ctx: dict[str, Any], high: str = "high") -> str:
    """Downgrade an absence-based gap to medium when history is only partial."""
    return "medium" if ctx.get("tool_history_partial") else high


def _flag_present(tool_history: list[dict[str, Any]], command: str, flags: set[str]) -> bool:
    """True if any ``command`` entry carries one of ``flags`` in its key options."""
    lowered = {f.lower() for f in flags}
    for entry in tool_history:
        if str(entry.get("command") or "").lower() != command:
            continue
        opts = {str(o).lower() for o in entry.get("key_options") or []}
        if opts & lowered:
            return True
    return False


def _recon_rules(ctx: dict[str, Any], th: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    targets: set[str] = set(ctx.get("target_types") or [])

    if "web_application" in targets and not (
        has_tool(th, _RECON_WEB) or ctx.get("proxy_sitemap_available")
    ):
        gaps.append(
            make_gap(
                rule_key="recon_web",
                area_key="web_path_discovery",
                priority=_absence_priority(ctx),
                area="Web path discovery",
                reason="A web target is in scope but no crawling or path-discovery activity "
                "was recorded.",
                suggested_action="Run path/content discovery (katana, ffuf, dirsearch, or a "
                "proxy sitemap crawl) and validate discovered endpoints.",
                evidence=["target:web_application", "tool_history:no-path-discovery"],
            )
        )

    if "ip_address" in targets and not has_tool(th, _RECON_IP):
        gaps.append(
            make_gap(
                rule_key="recon_ip",
                area_key="port_service_discovery",
                priority=_absence_priority(ctx),
                area="Port and service discovery",
                reason="An IP target is in scope but no port/service discovery was recorded.",
                suggested_action="Run port and service discovery (nmap or naabu) before "
                "concluding the assessment.",
                evidence=["target:ip_address", "tool_history:no-port-scan"],
            )
        )

    if (targets & {"repository", "local_code"}) and not has_tool(th, _SOURCE_TRIAGE):
        gaps.append(
            make_gap(
                rule_key="recon_source",
                area_key="source_triage",
                priority=_absence_priority(ctx),
                area="Source triage",
                reason="A source target is in scope but no source triage (SAST/secret scan) "
                "was recorded.",
                suggested_action="Run source triage (semgrep, gitleaks/trufflehog, trivy fs) "
                "over the codebase.",
                evidence=["target:source", "tool_history:no-source-triage"],
            )
        )
    return gaps


def _cve_rule(ctx: dict[str, Any], th: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: set[str] = set(ctx.get("target_types") or [])
    signals: list[str] = ctx.get("signal_text") or []
    is_source = bool(targets & {"repository", "local_code"})
    version_signal = has_signal(
        signals, {"package.json", "requirements.txt", "version", "framework"}
    )
    if not (is_source or version_signal):
        return []
    if has_tool(th, _CVE_TOOLS) or _has_evidence_keyword(th, {"audit", "cve"}):
        return []
    exposed = "web_application" in targets or has_signal(signals, {"admin", "upload", "auth"})
    base = "high" if exposed else "medium"
    return [
        make_gap(
            rule_key="cve_dependency",
            area_key="dependency_cve",
            priority=_absence_priority(ctx, base),
            area="Dependency and CVE coverage",
            reason="Source or version/package signals were observed but no dependency/CVE "
            "check was recorded.",
            suggested_action="Run a dependency/CVE check (trivy, npm audit, pip-audit, retire) "
            "or a targeted CVE lookup for detected components.",
            evidence=["signal:dependency", "tool_history:no-cve-check"],
        )
    ]


def _attack_vector_rules(ctx: dict[str, Any], th: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[str] = ctx.get("signal_text") or []
    gaps: list[dict[str, Any]] = []

    if has_signal(signals, {"graphql"}) and not _has_evidence_keyword(th, {"graphql"}):
        gaps.append(
            make_gap(
                rule_key="attack_graphql",
                area_key="graphql",
                priority=_absence_priority(ctx),
                area="GraphQL",
                reason="A GraphQL surface was observed but no GraphQL-specific testing was "
                "recorded.",
                suggested_action="Run GraphQL checks: introspection, batching, depth/alias "
                "abuse, and field-level authorisation.",
                evidence=["signal:graphql", "tool_history:no-graphql-testing"],
                suggested_skills=["graphql"],
            )
        )

    jwt_signals = {"jwt", "bearer", "oauth", "sso", "access_token", "id_token"}
    if has_signal(signals, jwt_signals) and not _has_evidence_keyword(th, {"jwt", "jwt_tool"}):
        gaps.append(
            make_gap(
                rule_key="auth_jwt",
                area_key="jwt_authentication",
                priority=_absence_priority(ctx),
                area="JWT authentication",
                reason="JWT/session token handling was observed, but no JWT-specific "
                "validation was recorded.",
                suggested_action="Run focused JWT validation for algorithm confusion, weak "
                "secrets, expiry, and claim tampering.",
                evidence=["signal:jwt", "tool_history:no-jwt-testing"],
                suggested_skills=["authentication_jwt"],
            )
        )

    upload_signals = {"upload", "avatar", "attachment", "document", "/import"}
    if has_signal(signals, upload_signals) and not _has_evidence_keyword(
        th, {"upload", "fuxploider"}
    ):
        exposed = has_signal(signals, {"admin", "auth"})
        gaps.append(
            make_gap(
                rule_key="attack_upload",
                area_key="file_upload",
                priority=_absence_priority(ctx, "high" if exposed else "medium"),
                area="File upload handling",
                reason="A file upload/handling surface was observed but no upload-bypass "
                "testing was recorded.",
                suggested_action="Test file-upload restrictions, content-type/extension "
                "bypasses, and downstream file handling.",
                evidence=["signal:upload", "tool_history:no-upload-testing"],
            )
        )

    access_signals = {"admin", "/users", "account", "organisation", "organization", "tenant",
                      "user_id", "userid"}
    access_evidence = {"idor", "autorize", "bola", "bfla", "access-control", "access_control"}
    if has_signal(signals, access_signals) and not (
        _has_evidence_keyword(th, access_evidence) or has_signal(signals, access_evidence)
    ):
        gaps.append(
            make_gap(
                rule_key="access_control",
                area_key="access_control_idor",
                priority=_absence_priority(ctx),
                area="Access control / IDOR",
                reason="Admin/user/tenant/object-id surfaces were observed but no "
                "access-control/IDOR validation was recorded.",
                suggested_action="Test object-level (IDOR) and function-level (BFLA) access "
                "control across roles and identifiers.",
                evidence=["signal:access-control", "tool_history:no-idor-testing"],
            )
        )
    return gaps


def _tool_option_rules(ctx: dict[str, Any], th: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Medium, non-blocking option gaps — only when the tool was actually used."""
    targets: set[str] = set(ctx.get("target_types") or [])
    signals: list[str] = ctx.get("signal_text") or []
    gaps: list[dict[str, Any]] = []

    if (
        "ip_address" in targets
        and has_shell_command(th, {"nmap"})
        and not _flag_present(th, "nmap", {"-sv", "-sc", "-a", "--version-all"})
    ):
        gaps.append(
            make_gap(
                rule_key="option_nmap",
                area_key="nmap_service_detection",
                priority="medium",
                area="nmap service/version detection",
                reason="nmap ran against an IP target without service/version detection.",
                suggested_action="Re-run nmap with -sV (and -sC) for service/version detection.",
                evidence=["tool_history:nmap-no-sv"],
            )
        )

    has_tech = has_signal(signals, {"framework", "version", "technology", "wordpress", "nginx",
                                    "apache", "tomcat"})
    if (
        has_tech
        and has_shell_command(th, {"nuclei"})
        and not _flag_present(th, "nuclei", {"-t", "-tags", "-templates", "-as"})
    ):
        gaps.append(
            make_gap(
                rule_key="option_nuclei",
                area_key="nuclei_templates",
                priority="medium",
                area="nuclei technology templates",
                reason="nuclei ran with default templates despite known technology signals.",
                suggested_action="Re-run nuclei with technology-specific -t/-tags for detected "
                "components.",
                evidence=["tool_history:nuclei-default"],
            )
        )

    if (
        has_signal(signals, {".php", ".asp", ".jsp", "file-like", ".bak", "backup"})
        and has_shell_command(th, {"ffuf"})
        and not _flag_present(th, "ffuf", {"-e", "-extensions"})
    ):
        gaps.append(
            make_gap(
                rule_key="option_ffuf",
                area_key="ffuf_extensions",
                priority="medium",
                area="ffuf file extensions",
                reason="ffuf ran without file extensions on a target that appears to serve "
                "files.",
                suggested_action="Re-run ffuf with -e for likely file extensions.",
                evidence=["tool_history:ffuf-no-extensions"],
            )
        )
    return gaps


def _diagnostic_gaps(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    if not ctx.get("tool_history_available"):
        return [
            make_gap(
                rule_key="diagnostic_tool_history",
                area_key="unavailable",
                priority="low",
                area="Tool history unavailable",
                reason="No attached agent sessions were available, so tool evidence could not "
                "be inspected. Absence-based gaps were suppressed.",
                suggested_action="Confirm coverage manually; tool-history-based checks could "
                "not run.",
                evidence=["diagnostic:tool-history-unavailable"],
            )
        ]
    if ctx.get("tool_history_partial"):
        return [
            make_gap(
                rule_key="diagnostic_tool_history",
                area_key="partial",
                priority="low",
                area="Tool history partial",
                reason="Tool history was only partially available; some sessions failed to "
                "parse, so absence-based gaps were downgraded.",
                suggested_action="Treat absence-based findings as indicative, not "
                "authoritative.",
                evidence=["diagnostic:tool-history-partial"],
            )
        ]
    return []


def evaluate_qa_gaps(review_context: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate all QA rules and return gaps sorted by priority (critical→low)."""
    th: list[dict[str, Any]] = review_context.get("tool_history") or []
    available = bool(review_context.get("tool_history_available"))

    gaps: list[dict[str, Any]] = []
    if available:
        gaps.extend(_recon_rules(review_context, th))
        gaps.extend(_cve_rule(review_context, th))
        gaps.extend(_attack_vector_rules(review_context, th))
    # Tool-option rules require the tool to be present, so they are safe even
    # when overall availability is borderline.
    gaps.extend(_tool_option_rules(review_context, th))
    gaps.extend(_diagnostic_gaps(review_context))

    gaps.sort(key=lambda g: _PRIORITY_RANK.get(g.get("priority", "low"), 99))
    return gaps


def assemble_review(
    gaps: list[dict[str, Any]],
    *,
    acknowledged_gaps: list[str],
    max_priority_gaps: int,
) -> dict[str, Any]:
    """Partition gaps into priority/residual and compute ``ready_to_finish``.

    Acknowledged ids and all medium/low gaps land in ``deferred_or_residual``;
    only unacknowledged high/critical gaps block and populate ``priority_gaps``
    (capped at ``max_priority_gaps``; ``priority_gaps_truncated`` reports how
    many blocking gaps were omitted by the cap). ``ready_to_finish`` is true
    when no unacknowledged blocking gap remains (computed before the cap).
    """
    ack = set(acknowledged_gaps)
    priority: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []

    for gap in gaps:
        gid = gap.get("gap_id", "")
        is_blocking = gap.get("priority") in _BLOCKING_PRIORITIES
        if gid in ack:
            residual.append({"gap_id": gid, "area": gap.get("area"), "reason": gap.get("reason"),
                             "priority": gap.get("priority"), "acknowledged": True})
        elif is_blocking:
            priority.append(gap)
        else:
            residual.append({"gap_id": gid, "area": gap.get("area"), "reason": gap.get("reason"),
                             "priority": gap.get("priority")})

    ready = len(priority) == 0
    capped = max(0, max_priority_gaps)
    return {
        "ready_to_finish": ready,
        "priority_gaps": priority[:capped],
        "priority_gaps_truncated": max(0, len(priority) - capped),
        "deferred_or_residual": residual,
        "acknowledged_gaps": sorted(ack),
    }
