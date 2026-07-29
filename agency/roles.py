"""Agency role implementations with model adapter integration.

Each role receives a deep-copied CTX view and returns a schema-validated
RoleResult. Roles never mutate the CTX directly.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import importlib.util
import subprocess
import sys
import uuid as _uuid
from pathlib import Path
from typing import Any

from .model_client import RoleModelAdapter, ModelCallResult

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Role result
# ---------------------------------------------------------------------------

VALID_STATUSES = frozenset({
    "COMPLETE", "NOOP", "DELEGATE", "NEED_CONTEXT", "ESCALATE", "FAIL_CLOSED",
})


class RoleResult:
    def __init__(self, role: str, status: str,
                 data: dict[str, Any] | None = None,
                 delegate_to: str = "", delegate_reason: str = "",
                 escalation_reason: str = "", fail_reason: str = "",
                 provenance: list[str] | None = None,
                 token_estimate: int = 0, cost_estimate: float = 0.0) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid role status: {status}")
        if token_estimate < 0 or cost_estimate < 0:
            raise ValueError("token/cost cannot be negative")
        if status == "DELEGATE" and (not delegate_to or not delegate_reason):
            raise ValueError("DELEGATE requires delegate_to and delegate_reason")
        if status == "ESCALATE" and not escalation_reason:
            raise ValueError("ESCALATE requires escalation_reason")
        if status == "FAIL_CLOSED" and not fail_reason:
            raise ValueError("FAIL_CLOSED requires fail_reason")
        self.role = role
        self.status = status
        self.timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.data = data or {}
        self.delegate_to = delegate_to
        self.delegate_reason = delegate_reason
        self.escalation_reason = escalation_reason
        self.fail_reason = fail_reason
        self.provenance = provenance or []
        self.token_estimate = token_estimate
        self.cost_estimate = cost_estimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "status": self.status,
            "timestamp": self.timestamp,
            "data": copy.deepcopy(self.data),
            "delegate_to": self.delegate_to,
            "delegate_reason": self.delegate_reason,
            "escalation_reason": self.escalation_reason,
            "fail_reason": self.fail_reason,
            "provenance": list(self.provenance),
            "token_estimate": self.token_estimate,
            "cost_estimate": self.cost_estimate,
        }


def _safe_result(role: str, result: ModelCallResult,
                 is_write_critical: bool = False) -> RoleResult:
    if result.success:
        return RoleResult(role, "COMPLETE", data=result.data,
                          token_estimate=result.total_tokens,
                          cost_estimate=result.estimated_cost)
    if is_write_critical or result.error_kind in ("missing_key", "transport"):
        return RoleResult(role, "FAIL_CLOSED", fail_reason=f"Model error: {result.error}")
    if result.error_kind == "empty":
        return RoleResult(role, "NOOP")
    return RoleResult(role, "FAIL_CLOSED", fail_reason=f"Model error: {result.error}")


# ---------------------------------------------------------------------------
# Moltbook read client (injectable)
# ---------------------------------------------------------------------------

class MoltbookReadClient:
    """Official-interface Moltbook read client. Injectable for tests."""

    @staticmethod
    def _get_client():
        """Load and return a MoltbookClient via direct module import."""
        _module_name = 'moltbook_write'
        if _module_name not in sys.modules:
            _spec = importlib.util.spec_from_file_location(
                _module_name,
                str(Path(__file__).resolve().parents[1] / 'scripts' / 'moltbook_write.py'))
            _mod = importlib.util.module_from_spec(_spec)
            sys.modules[_module_name] = _mod
            _spec.loader.exec_module(_mod)
        return sys.modules[_module_name].MoltbookClient()

    def fetch_post(self, post_id: str) -> dict[str, Any]:
        return self._get_client().fetch_post(post_id)

    def fetch_comments(self, post_id: str) -> dict[str, Any]:
        return self._get_client().fetch_comments(post_id)


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------

class ScoutRole:
    ROLE = "scout"

    def __init__(self, adapter: RoleModelAdapter | None = None,
                 moltbook: MoltbookReadClient | None = None) -> None:
        self._adapter = adapter
        self._moltbook = moltbook

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        inbox = ctx_view.get("inbox", [])
        known = set(ctx_view.get("known_ids", ctx_view.get("accepted_evidence_ids", [])))

        # Bounded Moltbook read if reader is available and inbox is empty
        if self._moltbook and not inbox:
            target_id = ctx_view.get("campaign", {}).get("active_inquiry", "")
            if target_id:
                try:
                    post = self._moltbook.fetch_post(target_id)
                    comments_resp = self._moltbook.fetch_comments(target_id)
                    items: list[dict[str, Any]] = []
                    # Add post itself
                    p = post.get("post", post)
                    if isinstance(p, dict) and p.get("id"):
                        content = p.get("content", "")[:500]
                        items.append({
                            "id": p["id"], "url": f"https://www.moltbook.com/post/{p['id']}",
                            "author_handle": p.get("author", {}).get("name", "unknown"),
                            "content_type": "post", "untrusted": True,
                            "content_excerpt": content,
                        })
                    # Add comments
                    for c in comments_resp.get("comments", []):
                        if isinstance(c, dict) and c.get("id"):
                            content = c.get("content", "")[:500]
                            items.append({
                                "id": c["id"],
                                "url": f"https://www.moltbook.com/post/{target_id}#comments",
                                "author_handle": c.get("author", {}).get("name", "unknown"),
                                "content_type": "comment", "untrusted": True,
                                "content_excerpt": content,
                            })
                    inbox = items
                except Exception as exc:
                    return RoleResult(self.ROLE, "FAIL_CLOSED",
                                      fail_reason=f"Moltbook read failed: {exc}")

        new = [i for i in inbox if i.get("id") not in known]
        if not new:
            return RoleResult(self.ROLE, "NOOP", data={"candidates_found": 0})

        if self._adapter:
            result = self._adapter.invoke({**ctx_view, "new_candidates": new})
            role_result = _safe_result(self.ROLE, result)
            # The model may select, sort, or score candidates but must NOT
            # replace the canonical source envelope.  Match every model
            # candidate back to its canonical inbox record by id/url to
            # preserve content_excerpt, untrusted, and author_handle.
            canonical_by_key: dict[str, dict[str, Any]] = {}
            for c in new:
                cid = c.get("id") or c.get("url", "")
                if cid:
                    canonical_by_key[cid] = c
            if role_result.status == "COMPLETE" and "candidates" in role_result.data:
                merged = []
                seen = set()
                for mc in role_result.data["candidates"]:
                    key = mc.get("id") or mc.get("url", "")
                    if key in seen:
                        continue
                    seen.add(key)
                    orig = canonical_by_key.get(key)
                    if orig:
                        # Restore every canonical field from the API record
                        merged.append({
                            "id": orig.get("id", mc.get("id", "")),
                            "url": orig.get("url", mc.get("url", "")),
                            "author_handle": orig.get("author_handle", mc.get("author_handle", "")),
                            "content_type": orig.get("content_type", mc.get("content_type", "")),
                            "untrusted": orig.get("untrusted", True),
                            "content_excerpt": orig.get("content_excerpt", ""),
                        })
                    else:
                        merged.append(mc)
                role_result.data["candidates"] = merged
                role_result.data["candidates_found"] = len(merged)
            return role_result

        return RoleResult(self.ROLE, "COMPLETE",
                          data={"candidates_found": len(new), "candidates": new},
                          provenance=[c.get("url", "") for c in new])


# ---------------------------------------------------------------------------
# Records Clerk
# ---------------------------------------------------------------------------

class RecordsClerkRole:
    ROLE = "records_clerk"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        candidates = ctx_view.get("source_candidates", [])
        if not candidates:
            return RoleResult(self.ROLE, "NOOP")

        if self._adapter:
            result = self._adapter.invoke({**ctx_view, "candidates_for_normalization": candidates})
            return _safe_result(self.ROLE, result)

        normalized = []
        for c in candidates:
            normalized.append({
                "source_id": c.get("id", c.get("source_id", "")),
                "url": c.get("url", ""), "author_handle": c.get("author_handle", "unknown"),
                "content_type": c.get("content_type", "unknown"), "untrusted": True,
                "observed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "content_excerpt": c.get("content_excerpt", ""),
                "paraphrase": c.get("paraphrase", ""), "provenance": [c.get("url", "")],
            })
        return RoleResult(self.ROLE, "COMPLETE", data={"normalized": normalized})


# ---------------------------------------------------------------------------
# Evidence Analyst
# ---------------------------------------------------------------------------

class EvidenceAnalystRole:
    ROLE = "evidence_analyst"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        candidates = ctx_view.get("source_candidates", [])
        if not candidates:
            return RoleResult(self.ROLE, "NOOP")

        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            return _safe_result(self.ROLE, result)

        accepted, rejected = [], []
        for c in candidates:
            if c.get("untrusted") is True:
                accepted.append(c)
        return RoleResult(self.ROLE, "COMPLETE",
                          data={"accepted": accepted, "rejected": rejected,
                                "claims": [], "scores": {},
                                "rationale": "Deterministic accept"},
                          provenance=[c.get("url", "") for c in accepted])


# ---------------------------------------------------------------------------
# Agency Director
# ---------------------------------------------------------------------------

class AgencyDirectorRole:
    ROLE = "agency_director"
    VALID = frozenset({"NOOP", "RECORD_ONLY", "PROPOSE_ENGAGEMENT",
                       "PROPOSE_ENGINEERING_INTAKE", "READY_FOR_SYNTHESIS",
                       "ESCALATE_TO_HUMAN"})

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        budget = ctx_view.get("budget", {})
        if budget.get("role_calls_used", 0) >= budget.get("max_role_calls", 20):
            return RoleResult(self.ROLE, "FAIL_CLOSED", fail_reason="Budget exhausted")

        evidence = ctx_view.get("accepted_evidence", [])
        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Director model failed: {result.error}")
            d = result.data.get("disposition", "")
            if d not in self.VALID:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Invalid disposition: {d}")
            return RoleResult(self.ROLE, "COMPLETE", data=result.data,
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost)

        if evidence:
            return RoleResult(self.ROLE, "COMPLETE", data={"disposition": "RECORD_ONLY"})
        return RoleResult(self.ROLE, "NOOP", data={"disposition": "NOOP"})


# ---------------------------------------------------------------------------
# Engagement Lead
# ---------------------------------------------------------------------------

class EngagementLeadRole:
    ROLE = "engagement_lead"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        # Build from Director brief + evidence + inquiry
        evidence = ctx_view.get("accepted_evidence", [])
        decisions = ctx_view.get("decisions", [])
        campaign = ctx_view.get("campaign", {})
        target_id = campaign.get("active_inquiry", "")

        if not evidence and not decisions:
            return RoleResult(self.ROLE, "NOOP")

        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Engagement model failed: {result.error}")
            prop = result.data.get("proposal", result.data)
            prop.setdefault("proposal_id", _uuid.uuid4().hex[:12])
            prop.setdefault("target_content_id", target_id)
            prop.setdefault("approval_state", "draft")
            prop.setdefault("consumed", False)
            prop.setdefault("source_refs", [])
            prop.setdefault("base_sha", ctx_view.get("base_sha", ""))
            prop.setdefault("repository", "kimeisele/hermes-sankhya-25")
            from agency.artifact import canonical_hash
            prop["content_hash"] = canonical_hash(prop)
            return RoleResult(self.ROLE, "COMPLETE", data={"proposal": prop},
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost)

        # Deterministic fallback: create minimal proposal
        from agency.artifact import canonical_hash
        prop = {
            "proposal_id": _uuid.uuid4().hex[:12],
            "target_content_id": target_id,
            "payload": {},
            "approval_state": "draft",
            "consumed": False,
            "source_refs": [],
            "base_sha": ctx_view.get("base_sha", ""),
            "repository": "kimeisele/hermes-sankhya-25",
        }
        prop["content_hash"] = canonical_hash(prop)
        return RoleResult(self.ROLE, "COMPLETE", data={"proposal": prop})


# ---------------------------------------------------------------------------
# Bridge Executor — deterministic, wraps moltbook_write.py
# ---------------------------------------------------------------------------

class BridgeExecutorRole:
    ROLE = "bridge_executor"

    def __init__(self, subprocess_runner: Any = None) -> None:
        self._runner = subprocess_runner or subprocess

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        return RoleResult(self.ROLE, "NOOP",
                          data={"dry_run": True, "note": "Bridge not invoked in dry-run"})

    def execute_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a write through scripts/moltbook_write.py. Returns parsed result."""
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "moltbook_write.py")
        payload_str = json.dumps(payload)
        try:
            r = self._runner.run(
                ["python3", script, "create", payload_str],
                capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return {"error": r.stderr.strip(), "output": r.stdout.strip()}
            return json.loads(r.stdout.strip())
        except Exception as exc:
            return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------

class AuditorRole:
    ROLE = "auditor"

    def __init__(self, adapter: RoleModelAdapter | None = None,
                 pro_adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter
        self._pro_adapter = pro_adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        budget = ctx_view.get("budget", {})
        findings: list[str] = []
        if budget.get("role_calls_used", 0) >= budget.get("max_role_calls", 20):
            findings.append("budget_role_calls_exhausted")
        if budget.get("cost_estimate_used", 0) >= budget.get("max_cost_estimate", 5.0):
            findings.append("budget_cost_exhausted")
        txns = ctx_view.get("transactions", [])
        max_w = ctx_view.get("policy", {}).get("max_writes_per_run", 1)
        if len(txns) > max_w:
            findings.append(f"write_count_exceeded: {len(txns)}>{max_w}")
        passed = len(findings) == 0
        if findings and self._pro_adapter:
            result = self._pro_adapter.invoke({**ctx_view, "findings": findings})
            if result.success:
                return RoleResult(self.ROLE, "COMPLETE",
                                  data={"findings": findings, "passed": False,
                                        "escalation": result.data})
        return RoleResult(self.ROLE, "COMPLETE",
                          data={"findings": findings, "passed": passed})


# ---------------------------------------------------------------------------
# Engineering Planner
# ---------------------------------------------------------------------------

class EngineeringPlannerRole:
    ROLE = "engineering_planner"

    def __init__(self, adapter: RoleModelAdapter | None = None) -> None:
        self._adapter = adapter

    def __call__(self, ctx_view: dict[str, Any]) -> RoleResult:
        if not ctx_view.get("accepted_evidence"):
            return RoleResult(self.ROLE, "NOOP")
        if self._adapter:
            result = self._adapter.invoke(ctx_view)
            if not result.success:
                return RoleResult(self.ROLE, "FAIL_CLOSED",
                                  fail_reason=f"Planner model failed: {result.error}")
            return RoleResult(self.ROLE, "COMPLETE", data=result.data,
                              token_estimate=result.total_tokens,
                              cost_estimate=result.estimated_cost)
        return RoleResult(self.ROLE, "COMPLETE", data={"proposals_created": 0})
