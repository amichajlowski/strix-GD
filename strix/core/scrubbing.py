"""Minimal structured-secret scrubber for recovery metadata and evidence text.

No general PII or client-name detection — only the common structured secret
shapes the recovery specs call out. Benign text and file paths stay readable.
"""

from __future__ import annotations

import re


_REDACTED = "XXXX"

# Bounds the scrubbed error message persisted to agent metadata so we never
# store an unbounded provider response body.
MAX_MESSAGE_LEN = 500

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # basic-auth URL: scheme://user:pass@host -> scheme://XXXX@host
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\1{_REDACTED}@"),
    # JWT-shaped value (header.payload.signature, base64url) — before Bearer so
    # a bare JWT in free text is caught too.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), _REDACTED),
    # Authorization / Bearer
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]+"), f"Bearer {_REDACTED}"),
    (re.compile(r"(?i)\b(authorization)\b(\s*[:=]\s*).+"), rf"\1\2{_REDACTED}"),
    # Cookie / Set-Cookie header values (rest of the line)
    (re.compile(r"(?i)\b(set-cookie|cookie)\b(\s*[:=]\s*).+"), rf"\1\2{_REDACTED}"),
    # Named secret values in query/form/JSON: api_key=..., token: "...", password=...
    (
        re.compile(
            r"(?i)(\"?\b(?:api[_-]?key|access[_-]?token|token|password|passwd|secret|credential)"
            r"\b\"?\s*[:=]\s*\"?)([^\s\"',;&}]+)"
        ),
        rf"\1{_REDACTED}",
    ),
    # Bare provider tokens sometimes appear in SDK exception messages.
    (
        re.compile(
            r"(?i)\b(?:sk(?:-proj)?-[A-Za-z0-9_\-]{8,}|xox[baprs]-[A-Za-z0-9\-]{8,}|"
            r"gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"
        ),
        _REDACTED,
    ),
    # Common cloud key shapes (AWS access key id)
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _REDACTED),
)


def scrub_secrets(text: str | None) -> str:
    """Replace common structured secrets in ``text`` with ``XXXX``."""
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def scrub_message(text: str | None) -> str:
    """Scrub secrets and bound the result for storage in error metadata."""
    return scrub_secrets(text)[:MAX_MESSAGE_LEN]


if __name__ == "__main__":
    # ponytail: self-check for the security path — fails loudly if a pattern breaks.
    assert scrub_secrets("Authorization: Bearer abc123def") == "Authorization: XXXX"
    assert "abc123" not in scrub_secrets("Bearer abc123def")
    assert scrub_secrets("Cookie: session=deadbeef; csrf=99") == "Cookie: XXXX"
    assert scrub_secrets("api_key=supersecret123&x=1") == "api_key=XXXX&x=1"
    assert scrub_secrets('{"password": "hunter2"}') == '{"password": "XXXX"}'
    assert scrub_secrets("token: abc.def") == "token: XXXX"
    assert scrub_secrets("provider key sk-testvalue123 leaked") == "provider key XXXX leaked"
    assert "hunter2" not in scrub_secrets("password=hunter2")
    assert scrub_secrets("https://user:pass@example.test/x") == "https://XXXX@example.test/x"
    jwt = "eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJ"
    assert scrub_secrets(f"jwt {jwt}") == "jwt XXXX"
    assert scrub_secrets("key AKIAIOSFODNN7EXAMPLE here") == "key XXXX here"
    benign = "Failed to read /workspace/repo/app.py at line 42"
    assert scrub_secrets(benign) == benign
    assert len(scrub_message("x" * 9000)) == MAX_MESSAGE_LEN
    print("scrubbing self-check passed")  # noqa: T201
