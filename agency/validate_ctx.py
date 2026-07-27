"""Runtime CTX validation against committed schema."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "agency-context-v1.schema.json"


def validate_sanitized_ctx(ctx_dict: dict[str, Any]) -> list[str]:
    """Validate a sanitized CTX dict against the committed schema.

    Returns list of error messages (empty = valid). Also checks:
    - workflow_run_id is null or numeric
    - base_sha is 40 hex chars
    - status is valid
    """
    errors: list[str] = []

    # Schema validation
    schema = json.loads(_SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(instance=ctx_dict, schema=schema)
    except jsonschema.ValidationError as exc:
        errors.append(f"Schema: {exc.message}")

    # workflow_run_id: null or numeric
    wf_id = ctx_dict.get("workflow_run_id")
    if wf_id is not None:
        if not re.fullmatch(r"[0-9]+", str(wf_id)):
            errors.append(f"workflow_run_id must be numeric or null, got: {wf_id}")

    # base_sha: 40 hex chars
    sha = ctx_dict.get("base_sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        errors.append(f"base_sha must be 40 hex chars, got: {sha}")

    # status
    valid_statuses = {"initialized", "running", "completed", "failed", "budget_exhausted"}
    if ctx_dict.get("status") not in valid_statuses:
        errors.append(f"Invalid status: {ctx_dict.get('status')}")

    return errors
