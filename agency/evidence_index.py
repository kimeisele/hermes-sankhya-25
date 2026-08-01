"""Repository-owned evidence index loader.

Loads known Moltbook content IDs from committed source records under
sources/records/. Returns a validated set of content IDs for cross-run
deduplication. Never modifies source records.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_MOLTBOOK_URL_RE = re.compile(r"moltbook\.com/post/([a-f0-9-]+)")
# Moltbook content IDs are UUIDs (hex + hyphens)
_VALID_ID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


def load_evidence_index(repo_root: str | Path | None = None) -> set[str]:
    """Load known Moltbook content IDs from committed source records.

    Returns a set of content IDs derived from URL fields in source records.
    Fails closed on malformed records that claim B001 Moltbook evidence.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]
    records_dir = Path(repo_root) / "sources" / "records"

    ids: set[str] = set()
    if not records_dir.exists():
        return ids

    for record_file in sorted(records_dir.glob("src-b001-*.json")):
        try:
            data = json.loads(record_file.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed source record {record_file.name}: {exc}")

        if not isinstance(data, dict):
            raise ValueError(f"Source record {record_file.name} is not a JSON object")

        # Only process B001 Moltbook records
        inquiry_ids = data.get("inquiry_ids", [])
        if "B001" not in inquiry_ids:
            continue

        content_type = data.get("content_type", "")
        if content_type not in ("post", "comment"):
            continue

        url = data.get("url", "")
        if not url:
            raise ValueError(f"B001 source record {record_file.name} missing URL")

        # Extract Moltbook content ID from URL
        match = _MOLTBOOK_URL_RE.search(url)
        if not match:
            raise ValueError(
                f"B001 source record {record_file.name} has non-Moltbook URL: {url}")

        content_id = match.group(1)
        if content_id:
            ids.add(content_id)

        # Comment records may additionally carry an explicit comment_id
        # (a known external comment).  Parent-post ID from the URL is
        # still indexed; the comment ID is the dedup key for the Scout.
        if content_type == "comment":
            comment_id = data.get("comment_id", "")
            if comment_id:
                # comment_id must be a non-empty, well-formed Moltbook UUID
                if not isinstance(comment_id, str) or not comment_id.strip():
                    raise ValueError(
                        f"B001 source record {record_file.name} has malformed comment_id")
                if not _VALID_ID_RE.fullmatch(comment_id):
                    raise ValueError(
                        f"B001 source record {record_file.name} has malformed comment_id: "
                        f"{comment_id}")
                ids.add(comment_id)

        # Also add explicit source_id if present
        source_id = data.get("source_id", "")
        if source_id and source_id.startswith("src-"):
            pass  # source_id is a repo-local identifier, not a Moltbook ID

    return ids
