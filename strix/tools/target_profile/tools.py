"""Per-target fingerprint store — mirrored to {state_dir}/target_profiles.json.

A small per-target fingerprint object recon writes once and every specialist
reads before composing a tool command. It answers "what is this target?" so
tool options stop being one-size-fits-all — never fuzz localhost slowly, never
spray raw payloads through a WAF.

The functional core is the pure ``profile_to_hints`` mapper: it turns
fingerprint facts into concrete, tool-agnostic tuning hints (not commands). The
model still composes the command; the hints steer it. ``classify_network_location``
is likewise pure — string/IP parsing only, no DNS, no network calls.

Mirrors ``strix/tools/notes/tools.py`` lifecycle: in-memory dict keyed by target
string, ``RLock``, atomic persist, hydrate on resume, visible to every agent in
the run. Profiles hold no secrets, so persistence uses the notes-style 0644
atomic write (loot's 0o600 discipline is reserved for the secret store).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agents import RunContextWrapper, function_tool


logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Enums (validated on set)
# --------------------------------------------------------------------------- #

_VALID_NETWORK_LOCATION = ["localhost", "internal", "internet", "unknown"]
_VALID_AUTH_MODEL = ["none", "cookie", "bearer", "basic", "unknown"]
_VALID_ASSET_TYPE = ["web_app", "spa", "api_rest", "api_graphql", "unknown"]
_VALID_CLOUD_PROVIDER = ["aws", "gcp", "azure", "none", "unknown"]
_VALID_SCOPE_SIZE = ["single_host", "small", "large", "unknown"]

# field name -> valid value set (used for enum validation on upsert)
_ENUM_FIELDS: dict[str, list[str]] = {
    "network_location": _VALID_NETWORK_LOCATION,
    "auth_model": _VALID_AUTH_MODEL,
    "asset_type": _VALID_ASSET_TYPE,
    "cloud_provider": _VALID_CLOUD_PROVIDER,
    "scope_size": _VALID_SCOPE_SIZE,
}

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

_MAX_NOTES_LEN = 512
_MAX_TECH_STACK = 32
_MAX_PORTS = 32
_MAX_SOURCES = 32
_MAX_ITEM_LEN = 120
_MAX_TOTAL_PROFILES = 1000

# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_target_profiles: dict[str, dict[str, Any]] = {}
_target_profiles_lock = threading.RLock()
_target_profiles_path: Path | None = None


# --------------------------------------------------------------------------- #
# Pure helpers (the main unit-test targets — no network, no DNS)
# --------------------------------------------------------------------------- #


def _extract_host(host_or_url: str) -> str:
    """Strip scheme/path/port and return the bare host, or '' if none."""
    raw = (host_or_url or "").strip()
    if not raw:
        return ""
    # If a scheme is present, urlsplit gives us a clean netloc; otherwise fall
    # back to treating the whole string as an authority-ish token.
    if "://" in raw:
        parsed = urlsplit(raw)
        netloc = parsed.netloc or parsed.path
    else:
        netloc = urlsplit(f"//{raw}").netloc or raw
    # Strip userinfo.
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    # Bracketed IPv6 literal, e.g. [::1]:8443.
    if netloc.startswith("["):
        end = netloc.find("]")
        if end != -1:
            return netloc[1:end]
        return netloc.strip("[]")
    # Strip a trailing :port (only for host:port, not bare IPv6).
    if netloc.count(":") == 1:
        netloc = netloc.split(":", 1)[0]
    return netloc.strip()


def classify_network_location(host_or_url: str) -> str:
    """Classify a host/URL: localhost | internal | internet | unknown.

    Pure string/IP parsing — no DNS, no network calls.

    - ``localhost``, ``*.localhost``, ``*.local``, ``127.0.0.0/8``, ``::1``
      -> ``localhost``
    - RFC1918 (``10/8``, ``172.16/12``, ``192.168/16``) and RFC4193
      (``fc00::/7`` ULA) -> ``internal``
    - any other valid public host/IP -> ``internet``
    - empty / unparseable garbage -> ``unknown``
    """
    host = _extract_host(host_or_url)
    if not host:
        return "unknown"

    # IP literal? Classify precisely via the stdlib.
    ip_class = _classify_ip(host)
    if ip_class is not None:
        return ip_class

    # Hostname (not an IP).
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith((".local", ".localhost")):
        return "localhost"

    # A plausible public hostname has at least one dot and only DNS-legal
    # characters in its labels. Anything else is garbage -> unknown.
    labels = lowered.split(".")
    if len(labels) >= 2 and all(_is_valid_label(label) for label in labels):
        return "internet"

    return "unknown"


def _classify_ip(host: str) -> str | None:
    """Classify an IP literal, or return None if ``host`` is not an IP."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_loopback:
        return "localhost"
    if ip.is_private:
        # RFC1918 + RFC4193 (ULA) are both covered by is_private.
        return "internal"
    return "internet"


def _is_valid_label(label: str) -> bool:
    """A DNS label: 1-63 chars, alphanumerics/hyphen, not edge-hyphenated."""
    if not (1 <= len(label) <= 63):
        return False
    if label.startswith("-") or label.endswith("-"):
        return False
    return all(ch.isalnum() or ch == "-" for ch in label)


def profile_to_hints(profile: dict[str, Any]) -> dict[str, Any]:
    """Map fingerprint facts to concrete, tool-agnostic tuning hints.

    Mapping rules (kept small):

    - ``network_location in (localhost, internal)`` -> ``throughput: high``,
      ``host_timeout: short``, ``evasion: none``.
    - ``waf`` set (not none/unknown/None) -> ``throughput: low``,
      ``evasion: encode+rotate-headers``, and advise ``traffic_health`` monitoring.
    - ``rate_limit_observed is True`` -> ``throughput: low``.
    - ``asset_type == api_graphql`` -> ``skills: ["graphql"]``; ``api_rest`` ->
      suggest ``arjun``/param discovery; ``spa`` -> JS-aware crawl.
    - ``cloud_provider in (aws, gcp, azure)`` -> note the metadata endpoint as
      an SSRF target.
    - unknown / empty -> conservative defaults (``throughput: moderate``,
      ``evasion: none``, ``host_timeout: normal``).

    Pure: never runs a tool.
    """
    profile = profile or {}
    network_location = profile.get("network_location")
    waf = profile.get("waf")
    rate_limited = profile.get("rate_limit_observed")
    asset_type = profile.get("asset_type")
    cloud_provider = profile.get("cloud_provider")

    # Conservative defaults.
    throughput = "moderate"
    evasion = "none"
    host_timeout = "normal"
    skills: list[str] = []
    notes_parts: list[str] = []

    # Network location.
    if network_location in ("localhost", "internal"):
        throughput = "high"
        host_timeout = "short"
        evasion = "none"
        notes_parts.append(
            f"{network_location} target: no WAF/rate-limit expected — "
            "safe to raise threads/rate and drop evasion."
        )

    # WAF/CDN presence dominates: force low + evasion.
    waf_present = waf not in (None, "none", "unknown", "")
    if waf_present:
        throughput = "low"
        evasion = "encode+rotate-headers"
        notes_parts.append(
            f"WAF/CDN present ({waf}): encode payloads, rotate headers, and "
            "watch traffic_health before scaling load."
        )

    # Observed rate limiting also lowers throughput.
    if rate_limited is True:
        throughput = "low"
        notes_parts.append("Rate limiting observed — keep throughput low.")

    # Asset type -> skills / discovery advice.
    if asset_type == "api_graphql":
        skills.append("graphql")
        notes_parts.append("GraphQL API — introspect the schema and abuse it.")
    elif asset_type == "api_rest":
        notes_parts.append("REST API — run parameter discovery (arjun) to widen surface.")
    elif asset_type == "spa":
        notes_parts.append("SPA — use a JS-aware crawl to reach client-rendered routes.")

    # Cloud provider -> SSRF metadata endpoint hint.
    if cloud_provider in ("aws", "gcp", "azure"):
        notes_parts.append(
            f"Hosted on {cloud_provider} — treat the cloud metadata endpoint as an SSRF target."
        )

    return {
        "throughput": throughput,
        "evasion": evasion,
        "host_timeout": host_timeout,
        "skills": skills,
        "notes": " ".join(notes_parts),
    }


# --------------------------------------------------------------------------- #
# Store lifecycle: hydrate and persist
# --------------------------------------------------------------------------- #


def hydrate_target_profiles_from_disk(state_dir: Path) -> None:
    global _target_profiles_path  # noqa: PLW0603
    _target_profiles_path = state_dir / "target_profiles.json"
    with _target_profiles_lock:
        _target_profiles.clear()
        if not _target_profiles_path.exists():
            return
        try:
            data = json.loads(_target_profiles_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "target_profiles.json at %s is unreadable; starting empty",
                _target_profiles_path,
            )
            return
        if not isinstance(data, dict):
            return
        _target_profiles.update(
            {
                target: profile
                for target, profile in data.items()
                if isinstance(target, str) and isinstance(profile, dict)
            }
        )
        logger.info(
            "target profiles hydrated from %s (%d profile(s))",
            _target_profiles_path,
            len(_target_profiles),
        )


def _persist() -> None:
    path = _target_profiles_path
    if path is None:
        return
    try:
        payload = json.dumps(_target_profiles, ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _target_profiles_lock,
            tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp,
        ):
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except Exception:
        logger.exception("target profiles persist to %s failed", path)


# --------------------------------------------------------------------------- #
# Bounds helpers
# --------------------------------------------------------------------------- #


def _bound_str_list(items: list[Any] | None, max_items: int, *, dedup: bool) -> list[Any]:
    """Bound each string item, cap the count, optionally dedup (order-preserving)."""
    seen: set[str] = set()
    result: list[Any] = []
    for raw in items or []:
        item = str(raw)[:_MAX_ITEM_LEN]
        if not item:
            continue
        if dedup:
            if item in seen:
                continue
            seen.add(item)
        result.append(item)
        if len(result) >= max_items:
            break
    return result


def _bound_ports(ports: list[int] | None) -> list[int]:
    result: list[int] = []
    for raw in ports or []:
        try:
            result.append(int(raw))
        except (TypeError, ValueError):
            continue
        if len(result) >= _MAX_PORTS:
            break
    return result


# --------------------------------------------------------------------------- #
# Impls
# --------------------------------------------------------------------------- #


def _merge_profile_fields(
    profile: dict[str, Any],
    *,
    network_location: str | None = None,
    scheme: str | None = None,
    ports: list[int] | None = None,
    waf: str | None = None,
    cdn: str | None = None,
    tech_stack: list[str] | None = None,
    auth_model: str | None = None,
    asset_type: str | None = None,
    cloud_provider: str | None = None,
    rate_limit_observed: bool | None = None,
    scope_size: str | None = None,
    notes: str | None = None,
    sources: list[str] | None = None,
) -> None:
    """Merge only the provided (non-None) fields into ``profile`` in place.

    Enum fields are already bounded by validation; free-text ones (scheme/waf/
    cdn) come from scanner banners, so bound them to keep untrusted text small
    before it can reach profile_to_hints or disk.
    """
    enum_str_fields = {
        "network_location": network_location,
        "auth_model": auth_model,
        "asset_type": asset_type,
        "cloud_provider": cloud_provider,
        "scope_size": scope_size,
    }
    profile.update({f: v for f, v in enum_str_fields.items() if v is not None})
    profile.update(
        {
            f: str(v)[:_MAX_ITEM_LEN]
            for f, v in {"scheme": scheme, "waf": waf, "cdn": cdn}.items()
            if v is not None
        }
    )
    if rate_limit_observed is not None:
        profile["rate_limit_observed"] = rate_limit_observed
    if notes is not None:
        profile["notes"] = str(notes)[:_MAX_NOTES_LEN]
    if tech_stack is not None:
        profile["tech_stack"] = _bound_str_list(tech_stack, _MAX_TECH_STACK, dedup=False)
    if ports is not None:
        profile["ports"] = _bound_ports(ports)
    if sources is not None:
        stored = profile.get("sources", [])
        if not isinstance(stored, list):
            stored = []
        profile["sources"] = _bound_str_list([*stored, *sources], _MAX_SOURCES, dedup=True)


def _set_target_profile_impl(
    target: str,
    network_location: str | None = None,
    scheme: str | None = None,
    ports: list[int] | None = None,
    waf: str | None = None,
    cdn: str | None = None,
    tech_stack: list[str] | None = None,
    auth_model: str | None = None,
    asset_type: str | None = None,
    cloud_provider: str | None = None,
    rate_limit_observed: bool | None = None,
    scope_size: str | None = None,
    notes: str | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Upsert a target profile, merging only the fields actually provided."""
    with _target_profiles_lock:
        try:
            if not target or not target.strip():
                return {"success": False, "error": "target cannot be empty"}
            target = target.strip()

            # Validate the enums up front — reject with the valid set listed.
            enum_values = {
                "network_location": network_location,
                "auth_model": auth_model,
                "asset_type": asset_type,
                "cloud_provider": cloud_provider,
                "scope_size": scope_size,
            }
            for field, value in enum_values.items():
                if value is not None and value not in _ENUM_FIELDS[field]:
                    valid = ", ".join(_ENUM_FIELDS[field])
                    return {
                        "success": False,
                        "error": f"Invalid {field} '{value}'. Must be one of: {valid}",
                    }

            if target not in _target_profiles and len(_target_profiles) >= _MAX_TOTAL_PROFILES:
                return {
                    "success": False,
                    "error": (
                        f"Target profile store is full ({_MAX_TOTAL_PROFILES} targets); "
                        "no room for a new target"
                    ),
                }

            profile = dict(_target_profiles.get(target, {}))  # copy — never mutate stored
            profile["target"] = target
            _merge_profile_fields(
                profile,
                network_location=network_location,
                scheme=scheme,
                ports=ports,
                waf=waf,
                cdn=cdn,
                tech_stack=tech_stack,
                auth_model=auth_model,
                asset_type=asset_type,
                cloud_provider=cloud_provider,
                rate_limit_observed=rate_limit_observed,
                scope_size=scope_size,
                notes=notes,
                sources=sources,
            )
            profile["updated_at"] = datetime.now(UTC).isoformat()
            _target_profiles[target] = profile
        except (ValueError, TypeError) as e:
            return {"success": False, "error": f"Failed to set target profile: {e}"}
        else:
            _persist()
            return {"success": True, "target": target, "profile": profile}


def _get_target_profile_impl(target: str | None = None) -> dict[str, Any]:
    """Return one profile (by target, with hints) or all profiles."""
    with _target_profiles_lock:
        if target is None:
            profiles = {t: dict(p) for t, p in _target_profiles.items()}
            return {
                "success": True,
                "profile": profiles,
                "count": len(profiles),
            }
        profile = _target_profiles.get(target)
        if profile is None:
            return {
                "success": False,
                "error": f"No profile for target '{target}'",
                "profile": None,
            }
        profile = dict(profile)
        return {
            "success": True,
            "profile": profile,
            "hints": profile_to_hints(profile),
        }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@function_tool(timeout=30)
async def set_target_profile(
    ctx: RunContextWrapper,
    target: str,
    network_location: str | None = None,
    scheme: str | None = None,
    ports: list[int] | None = None,
    waf: str | None = None,
    cdn: str | None = None,
    tech_stack: list[str] | None = None,
    auth_model: str | None = None,
    asset_type: str | None = None,
    cloud_provider: str | None = None,
    rate_limit_observed: bool | None = None,
    scope_size: str | None = None,
    notes: str | None = None,
    sources: list[str] | None = None,
) -> str:
    """Record what a target IS during recon — upsert with merge.

    Only the fields you pass are written; everything else is left untouched, so
    call it repeatedly as facts arrive. Every agent in the scan reads it before
    scaling load. Pass ``sources`` (the tools that fed the facts, e.g.
    ``["wafw00f"]``) — they are appended and deduped.

    Args:
        target: Host or base URL (the profile key).
        network_location: localhost | internal | internet | unknown.
        scheme: e.g. https.
        ports: Open/relevant ports.
        waf: WAF name, or "none"/"unknown".
        cdn: CDN name, or "none"/"unknown".
        tech_stack: Detected stack (e.g. from httpx -td).
        auth_model: none | cookie | bearer | basic | unknown.
        asset_type: web_app | spa | api_rest | api_graphql | unknown.
        cloud_provider: aws | gcp | azure | none | unknown.
        rate_limit_observed: True if rate limiting was seen.
        scope_size: single_host | small | large | unknown.
        notes: Short free-text context (bounded).
        sources: Tools that fed the facts (appended + deduped).
    """
    return json.dumps(
        await asyncio.to_thread(
            _set_target_profile_impl,
            target,
            network_location,
            scheme,
            ports,
            waf,
            cdn,
            tech_stack,
            auth_model,
            asset_type,
            cloud_provider,
            rate_limit_observed,
            scope_size,
            notes,
            sources,
        ),
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def get_target_profile(ctx: RunContextWrapper, target: str | None = None) -> str:
    """Read a target's fingerprint plus derived tuning hints before heavy tools.

    Pass a ``target`` for one profile (returned with ``hints`` — throughput,
    evasion, host_timeout, skills, notes — so you tune options in one call), or
    omit it to list every profile in the scan. Read this before running
    scanners/fuzzers: never fuzz localhost slowly, never spray raw payloads
    through a WAF.

    Args:
        target: Host or base URL to look up, or None for all profiles.
    """
    return json.dumps(
        await asyncio.to_thread(_get_target_profile_impl, target),
        ensure_ascii=False,
        default=str,
    )
