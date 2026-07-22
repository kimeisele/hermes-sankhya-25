"""Branch protection evaluation and baseline enforcement.

Reads rules from two API sources (ruleset endpoint + classic branch
protection), evaluates each baseline rule type independently, and
creates the federation-baseline ruleset when needed.

In v1, existing rulesets are **never** updated via PUT.
"""
from __future__ import annotations

import copy

from governance._models import (
    BypassState,
    ComplianceStatus,
    Diagnostic,
    GovernanceCheck,
    GovernanceResult,
    RepoInfo,
    RuleStatus,
)
from federation_utils import github_api

# ── Baseline definition ────────────────────────────────────────────────────

RULESET_NAME = "agent-federation-baseline-v1"
BASELINE_RULE_TYPES = frozenset({"deletion", "non_fast_forward", "pull_request"})

RULESET_PAYLOAD_V1: dict = {
    "name": RULESET_NAME,
    "target": "branch",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {
        "ref_name": {
            "include": ["~DEFAULT_BRANCH"],
            "exclude": [],
        },
    },
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["merge", "squash", "rebase"],
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_approving_review_count": 0,
                "required_review_thread_resolution": False,
            },
        },
    ],
}


# ── Public API ─────────────────────────────────────────────────────────────


def inspect_governance(repo: RepoInfo) -> GovernanceCheck:
    """Read both protection sources and evaluate against the baseline.

    This is read-only — no mutations are performed.
    """
    rules_data, protection_data, source_b_404, source_diag = _fetch_both_sources(repo)
    return _evaluate(repo, rules_data, protection_data, source_b_404, source_diag)


def ensure_governance_baseline(repo: RepoInfo, check: GovernanceCheck) -> GovernanceResult:
    """Create the federation-baseline ruleset if it is missing.

    In v1, existing rulesets are **never** updated via PUT.
    Returns a result with the action taken and a re-read final_check.
    """
    action, _create_diag = _ensure_baseline_ruleset(repo)

    # Re-read and verify — no local boolean as proof
    final_check: GovernanceCheck | None = None
    if action == "created":
        final_check = inspect_governance(repo)

    return GovernanceResult(check=check, action=action, final_check=final_check)


# ── Internal: data fetching ────────────────────────────────────────────────


def _fetch_both_sources(
    repo: RepoInfo,
) -> tuple[list[dict] | None, dict | None, bool, Diagnostic]:
    """Fetch rules from both API sources.

    Returns:
        ``(rules_list, protection_dict, source_b_was_404, diagnostic)``.

        *rules_list* — array from ``GET /rules/branches/{branch}``,
          ``None`` if source A is unreadable.
        *protection_dict* — body from ``GET /branches/{branch}/protection``,
          ``None`` if source B returned non-200.
        *source_b_was_404* — ``True`` if source B returned 404 specifically.
        *diagnostic* — cumulative diagnostic for the fetch step.
    """
    branch = repo.default_branch
    diag = Diagnostic.OK

    # Source A: ruleset rules
    rules_resp = github_api("GET", f"/repos/{repo.full_name}/rules/branches/{branch}")
    rules_data: list[dict] | None = None
    if rules_resp.status_code == 200 and isinstance(rules_resp.body, list):
        rules_data = rules_resp.body
    elif rules_resp.status_code in (401, 403):
        diag = _worse_diag(diag, _auth_diag(rules_resp.status_code))
    elif rules_resp.status_code != 200:
        diag = _worse_diag(diag, Diagnostic.GITHUB_UNREACHABLE)

    # Source B: classic branch protection
    prot_resp = github_api("GET", f"/repos/{repo.full_name}/branches/{branch}/protection")
    protection_data: dict | None = None
    source_b_404 = False
    if prot_resp.status_code == 200 and isinstance(prot_resp.body, dict):
        protection_data = prot_resp.body
    elif prot_resp.status_code == 404:
        source_b_404 = True  # No classic protection — normal, not an error
    elif prot_resp.status_code in (401, 403):
        diag = _worse_diag(diag, _auth_diag(prot_resp.status_code))
    elif prot_resp.status_code != 200:
        diag = _worse_diag(diag, Diagnostic.GITHUB_UNREACHABLE)

    return rules_data, protection_data, source_b_404, diag


# ── Internal: evaluation ───────────────────────────────────────────────────


def _evaluate(
    repo: RepoInfo,
    rules_data: list[dict] | None,
    protection_data: dict | None,
    source_b_404: bool,
    source_diag: Diagnostic,
) -> GovernanceCheck:
    """Evaluate baseline compliance from both sources."""
    rule_statuses: dict[str, RuleStatus] = {}
    details: list[str] = []

    for rule_type in sorted(BASELINE_RULE_TYPES):
        status = _rule_status_for_type(
            rule_type,
            rules_data,
            protection_data,
            source_b_404=source_b_404,
        )
        rule_statuses[rule_type] = status

    compliance = _overall_compliance(rule_statuses)

    present = [t for t, s in rule_statuses.items() if s == RuleStatus.CONFIRMED]
    missing = [t for t, s in rule_statuses.items() if s == RuleStatus.MISSING]
    unknown = [t for t, s in rule_statuses.items() if s == RuleStatus.UNKNOWN]

    diagnostics: list[Diagnostic] = []
    if source_diag != Diagnostic.OK:
        diagnostics.append(source_diag)
    if missing:
        details.append(f"Missing rules: {', '.join(missing)}")
    if unknown:
        details.append(f"Unknown rules: {', '.join(unknown)}")

    bypass_state = _determine_bypass_state(rules_data, protection_data, source_diag)

    return GovernanceCheck(
        compliance=compliance,
        diagnostics=diagnostics,
        repo_full_name=repo.full_name,
        default_branch=repo.default_branch,
        rule_statuses=rule_statuses,
        present_rules=present,
        missing_rules=missing,
        unknown_rules=unknown,
        bypass_state=bypass_state,
        details=details,
    )


def _rule_status_for_type(
    rule_type: str,
    rules_data: list[dict] | None,
    protection_data: dict | None,
    *,
    source_b_404: bool = False,
) -> RuleStatus:
    """Determine the status of a single baseline rule type from both sources.

    Source A (rulesets):
        data present → check ``type`` in list
        data is None → source is unreadable

    Source B (classic protection):
        data present → check specific fields
        data is None + source_b_404 → source readable, no classic prot exists
        data is None + not source_b_404 → source is unreadable

    Aggregation (per spec):
      1. CONFIRMED from any source → CONFIRMED
      2. No CONFIRMED, any source unreadable → UNKNOWN
      3. No CONFIRMED, all sources readable → MISSING
    """
    confirmed = False
    any_unreadable = False

    # ── Source A ──
    if rules_data is not None:
        if _rule_in_rules_list(rule_type, rules_data):
            confirmed = True
    else:
        any_unreadable = True

    # ── Source B ──
    if protection_data is not None:
        classic_status = _classic_confirms(rule_type, protection_data)
        if classic_status == RuleStatus.CONFIRMED:
            confirmed = True
        elif classic_status == RuleStatus.UNKNOWN:
            if not confirmed:
                any_unreadable = True
    elif not source_b_404:
        # protection_data is None, not because of 404 → source is unreadable
        if not confirmed:
            any_unreadable = True
    # else: source_b_404 and protection_data is None → no classic prot (no contribution)

    if confirmed:
        return RuleStatus.CONFIRMED
    if any_unreadable:
        return RuleStatus.UNKNOWN
    return RuleStatus.MISSING


def _rule_in_rules_list(rule_type: str, rules: list[dict]) -> bool:
    """Check whether *rule_type* appears in the rules/branches response array."""
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            return True
    return False


def _classic_confirms(rule_type: str, protection_data: dict) -> RuleStatus:
    """Evaluate whether classic branch protection confirms a rule type.

    Returns:
        CONFIRMED if the field is present and confirms the rule.
        MISSING if the field is present and explicitly does NOT confirm.
        UNKNOWN if the field is missing or not unambiguously evaluable.
    """
    if rule_type == "pull_request":
        if "required_pull_request_reviews" in protection_data:
            return RuleStatus.CONFIRMED
        return RuleStatus.UNKNOWN

    if rule_type == "non_fast_forward":
        afp = protection_data.get("allow_force_pushes")
        if isinstance(afp, dict) and "enabled" in afp:
            return RuleStatus.CONFIRMED if afp["enabled"] is False else RuleStatus.MISSING
        return RuleStatus.UNKNOWN

    if rule_type == "deletion":
        ad = protection_data.get("allow_deletions")
        if isinstance(ad, dict) and "enabled" in ad:
            return RuleStatus.CONFIRMED if ad["enabled"] is False else RuleStatus.MISSING
        return RuleStatus.UNKNOWN

    return RuleStatus.UNKNOWN


def _overall_compliance(rule_statuses: dict[str, RuleStatus]) -> ComplianceStatus:
    """Derive overall compliance from per-rule-type statuses."""
    if all(s == RuleStatus.CONFIRMED for s in rule_statuses.values()):
        return ComplianceStatus.CONFORMANT
    if any(s == RuleStatus.UNKNOWN for s in rule_statuses.values()):
        return ComplianceStatus.UNKNOWN
    return ComplianceStatus.NON_CONFORMANT


# ── Internal: baseline ruleset management ──────────────────────────────────


def _ensure_baseline_ruleset(repo: RepoInfo) -> tuple[str | None, Diagnostic]:
    """Create the baseline ruleset if it does not exist.

    In v1, this function **never** issues a PUT to update an existing
    ruleset.  It only creates via POST when the ruleset is absent.
    """
    # 1. List existing rulesets
    list_resp = github_api(
        "GET",
        f"/repos/{repo.full_name}/rulesets?includes_parents=false",
    )
    if list_resp.status_code != 200 or not isinstance(list_resp.body, list):
        return None, Diagnostic.GITHUB_UNREACHABLE

    # 2. Look for our ruleset by name
    existing = None
    for rs in list_resp.body:
        if isinstance(rs, dict) and rs.get("name") == RULESET_NAME:
            existing = rs
            break

    # 3. Ruleset fehlt → POST create
    if existing is None:
        payload = copy.deepcopy(RULESET_PAYLOAD_V1)
        create_resp = github_api(
            "POST",
            f"/repos/{repo.full_name}/rulesets",
            body=payload,
        )
        if create_resp.status_code == 201:
            return "created", Diagnostic.OK
        if create_resp.status_code in (401, 403):
            return None, _auth_diag(create_resp.status_code)
        return None, Diagnostic.API_ERROR

    # 4. Ruleset exists — check compatibility
    compatible, reason = _is_compatible(existing)

    if compatible:
        if reason == "stricter":
            return "skipped_conservative", Diagnostic.OK
        return "skipped", Diagnostic.OK

    # Incompatible / unknown → do not touch
    return None, Diagnostic.UNSUPPORTED_CONFIG


def _is_compatible(existing: dict) -> tuple[bool, str]:
    """Check whether an existing ruleset is compatible with the v1 baseline.

    Returns:
        ``(True, "exact")`` — baseline rules present, enforcement active.
        ``(True, "stricter")`` — baseline plus stricter rules, acceptable.
        ``(False, reason)`` — incompatible or unknown; do not touch.
    """
    # Enforcement must be active
    if existing.get("enforcement") != "active":
        return False, "enforcement_not_active"

    # Check for unexpected bypass actors
    bypass = existing.get("bypass_actors")
    if isinstance(bypass, list) and len(bypass) > 0:
        for ba in bypass:
            if isinstance(ba, dict):
                mode = ba.get("bypass_mode", "")
                if mode and mode != "none":
                    return False, "unexpected_bypass_actors"

    # Check baseline rule types are present
    existing_rules = existing.get("rules", [])
    if not isinstance(existing_rules, list):
        return False, "unknown_rules_structure"

    existing_types: set[str] = set()
    pull_request_count = 0
    for rule in existing_rules:
        if isinstance(rule, dict):
            rt = rule.get("type")
            if isinstance(rt, str):
                existing_types.add(rt)
                if rt == "pull_request":
                    params = rule.get("parameters", {})
                    if isinstance(params, dict):
                        pull_request_count = params.get("required_approving_review_count", 0)

    # Baseline rules must all be present
    if not BASELINE_RULE_TYPES.issubset(existing_types):
        missing = BASELINE_RULE_TYPES - existing_types
        return False, f"missing_baseline_rules:{','.join(sorted(missing))}"

    # Check for stricter rules beyond baseline
    if existing_types - set(BASELINE_RULE_TYPES):
        return True, "stricter"
    if isinstance(pull_request_count, int) and pull_request_count > 0:
        return True, "stricter"

    return True, "exact"


# ── Internal: helpers ──────────────────────────────────────────────────────


def _determine_bypass_state(
    rules_data: list[dict] | None,
    protection_data: dict | None,
    source_diag: Diagnostic,
) -> BypassState:
    """Determine bypass visibility from available data."""
    if source_diag in (Diagnostic.AUTH_MISSING, Diagnostic.PERMISSION_INSUFFICIENT):
        return BypassState.UNKNOWN

    # Check classic protection for bypass indicators
    if protection_data is not None:
        ea = protection_data.get("enforce_admins")
        if isinstance(ea, dict) and not ea.get("enabled", True):
            return BypassState.PRESENT
        if protection_data.get("restrictions") is not None:
            return BypassState.PRESENT

    # Both sources readable → no bypasses detected
    if rules_data is not None and protection_data is not None:
        return BypassState.NONE_CONFIRMED

    return BypassState.UNKNOWN


def _auth_diag(status_code: int) -> Diagnostic:
    """Map HTTP status to auth diagnostic."""
    if status_code == 401:
        return Diagnostic.AUTH_MISSING
    if status_code == 403:
        return Diagnostic.PERMISSION_INSUFFICIENT
    return Diagnostic.API_ERROR


def _worse_diag(a: Diagnostic, b: Diagnostic) -> Diagnostic:
    """Return the more severe of two diagnostics."""
    order = {
        Diagnostic.OK: 0,
        Diagnostic.API_ERROR: 1,
        Diagnostic.GITHUB_UNREACHABLE: 2,
        Diagnostic.PERMISSION_INSUFFICIENT: 3,
        Diagnostic.AUTH_MISSING: 4,
        Diagnostic.REPO_NOT_FOUND: 5,
        Diagnostic.UNSUPPORTED_CONFIG: 6,
    }
    return a if order.get(a, 0) >= order.get(b, 0) else b
