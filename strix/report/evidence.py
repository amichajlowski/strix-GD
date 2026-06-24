"""Small run-evidence manifest written before sandbox teardown.

Captures just enough to keep an audit recoverable and investigable without
copying container state: workspace source mapping, known report artefacts, the
Caido project URL, and the cleanup status. Structured secrets are scrubbed with
the shared helper; source paths and workspace mappings stay readable.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from strix.core.scrubbing import scrub_secrets


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

EVIDENCE_MANIFEST_FILENAME = "evidence_manifest.json"

_KNOWN_REPORT_FILES = ("vulnerabilities.json", "executive_report.md", "run.json")


def write_evidence_manifest(
    *,
    run_dir: Path,
    local_sources: list[dict[str, Any]],
    caido_url: str | None = None,
    sandbox_cleanup: str = "pending",
) -> None:
    """Write ``evidence_manifest.json`` under ``run_dir``.

    Best-effort: any failure is logged and swallowed so it never blocks sandbox
    cleanup or findings persistence.
    """
    try:
        manifest: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "sandbox_cleanup": sandbox_cleanup,
            "workspace_sources": [
                {
                    "workspace_subdir": src.get("workspace_subdir"),
                    "source_path": src.get("source_path"),
                    "mount": bool(src.get("mount")),
                }
                for src in local_sources
            ],
            "reports": [name for name in _KNOWN_REPORT_FILES if (run_dir / name).exists()],
        }
        if caido_url:
            manifest["caido_url"] = caido_url

        # Scrub via the shared helper (no second scrubber). Round-trips through
        # JSON so any credential embedded in a URL/value becomes XXXX while
        # benign paths and workspace mappings stay readable.
        scrubbed = json.loads(scrub_secrets(json.dumps(manifest)))
        (run_dir / EVIDENCE_MANIFEST_FILENAME).write_text(
            json.dumps(scrubbed, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Wrote evidence manifest to %s", run_dir / EVIDENCE_MANIFEST_FILENAME)
    except Exception:
        logger.exception("Failed to write evidence manifest under %s", run_dir)
