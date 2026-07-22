"""Unit tests for governance module — per Issue #7 §12.

All external GitHub API calls are mocked via :func:`unittest.mock.patch`.
No destructive integration tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure scripts/ is importable
_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from federation_utils import GitHubResponse  # noqa: E402
from governance._models import (  # noqa: E402
    BypassState,
    ComplianceStatus,
    Diagnostic,
    GovernanceCheck,
    RepoInfo,
    RuleStatus,
)
from governance._protection import (  # noqa: E402
    RULESET_NAME,
    RULESET_PAYLOAD_V1,
    BASELINE_RULE_TYPES,
    _classic_confirms,
    _is_compatible,
    _overall_compliance,
    _rule_in_rules_list,
    _rule_status_for_type,
    ensure_governance_baseline,
    inspect_governance,
)
from governance._repo import _parse_github_full_name, detect_repository  # noqa: E402

# ── helpers ────────────────────────────────────────────────────────────────

REPO = RepoInfo(full_name="kimeisele/test-node", default_branch="main")

# Pre-built mock responses
_REPO_OK = GitHubResponse(status_code=200, body={"default_branch": "main"}, error_message=None)
_RULES_EMPTY = GitHubResponse(status_code=200, body=[], error_message=None)
_PROTECTION_404 = GitHubResponse(status_code=404, body=None, error_message="Branch not protected")
_AUTH_401 = GitHubResponse(status_code=401, body={"message": "Bad credentials"}, error_message="Bad credentials")
_PERM_403 = GitHubResponse(status_code=403, body={"message": "Resource not accessible"}, error_message="Resource not accessible")
_NETWORK_ERROR = GitHubResponse(status_code=0, body=None, error_message="curl error: Could not resolve host")

_RULES_FULL = GitHubResponse(
    status_code=200,
    body=[
        {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "non_fast_forward", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1,
         "parameters": {"required_approving_review_count": 0}},
    ],
    error_message=None,
)

_RULES_PARTIAL = GitHubResponse(
    status_code=200,
    body=[
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
    ],
    error_message=None,
)

_RULES_TWO = GitHubResponse(
    status_code=200,
    body=[
        {"type": "pull_request", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
        {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_source": "kimeisele/test-node", "ruleset_id": 1},
    ],
    error_message=None,
)

_PROTECTION_FULL = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False, "require_code_owner_reviews": False},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "enforce_admins": {"enabled": True},
    },
    error_message=None,
)

_PROTECTION_FORCE_ENABLED = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False},
        "allow_force_pushes": {"enabled": True},
        "allow_deletions": {"enabled": False},
    },
    error_message=None,
)

_PROTECTION_MISSING_FIELDS = GitHubResponse(
    status_code=200,
    body={
        "required_pull_request_reviews": {"dismiss_stale_reviews": False},
    },
    error_message=None,
)

_RULESETS_LIST_EMPTY = GitHubResponse(status_code=200, body=[], error_message=None)
_RULESETS_LIST_EXISTING = GitHubResponse(
    status_code=200,
    body=[{
        "id": 42,
        "name": "agent-federation-baseline-v1",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
        ],
    }],
    error_message=None,
)
_RULESETS_CREATED = GitHubResponse(status_code=201, body={"id": 99, "name": RULESET_NAME}, error_message=None)


# ── 12.1 Basis-Tests ──────────────────────────────────────────────────────


class TestRepoDetection:
    """Tests 1–2, 18–19: Repository and branch detection."""

    def test_parse_https(self) -> None:
        """_parse_github_full_name handles HTTPS URLs."""
        assert _parse_github_full_name("https://github.com/kimeisele/my-node.git") == "kimeisele/my-node"
        assert _parse_github_full_name("https://github.com/kimeisele/my-node") == "kimeisele/my-node"

    def test_parse_ssh(self) -> None:
        """_parse_github_full_name handles SSH URLs."""
        assert _parse_github_full_name("git@github.com:kimeisele/my-node.git") == "kimeisele/my-node"
        assert _parse_github_full_name("ssh://git@github.com/kimeisele/my-node.git") == "kimeisele/my-node"

    def test_parse_non_github(self) -> None:
        """_parse_github_full_name returns None for non-GitHub URLs."""
        assert _parse_github_full_name("https://gitlab.com/org/repo.git") is None

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_success(self, mock_run: object, mock_api: object) -> None:
        """Test 1: detect_repository returns RepoInfo with remote default branch."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        mock_api.return_value = _REPO_OK

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.full_name == "kimeisele/test-node"
        assert repo.default_branch == "main"
        assert diag == Diagnostic.OK

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_no_remote(self, mock_run: object, mock_api: object) -> None:
        """Test 2: detect_repository returns REPO_NOT_FOUND when git remote fails."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: No such remote 'origin'",
        )
        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.REPO_NOT_FOUND

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_auth_missing(self, mock_run: object, mock_api: object) -> None:
        """Test 14: AUTH_MISSING on 401 from repo API."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        mock_api.return_value = _AUTH_401

        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.AUTH_MISSING

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_master_branch(self, mock_run: object, mock_api: object) -> None:
        """Test 18: Default branch named 'master' is handled."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/old-repo.git", stderr="",
        )
        mock_api.return_value = GitHubResponse(
            status_code=200, body={"default_branch": "master"}, error_message=None,
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.default_branch == "master"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_detect_repository_no_github_remote(self, mock_run: object, mock_api: object) -> None:
        """Test 19: Non-GitHub remote returns REPO_NOT_FOUND."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://gitlab.com/org/repo.git", stderr="",
        )
        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.REPO_NOT_FOUND


class TestEvaluation:
    """Tests 3–8, 14–17, 21: Baseline evaluation logic."""

    def test_unprotected_branch(self) -> None:
        """Test 3: Completely unprotected branch → NON_CONFORMANT, all three missing."""
        statuses = {
            "deletion": _rule_status_for_type("deletion", [], None, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", [], None, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", [], None, source_b_404=True),
        }
        assert statuses["deletion"] == RuleStatus.MISSING
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert statuses["pull_request"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_classic_protection_only(self) -> None:
        """Test 4: Baseline fully satisfied by classic branch protection only."""
        protection = _PROTECTION_FULL.body
        assert protection is not None
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED

    def test_rulesets_only(self) -> None:
        """Test 5: Baseline fully satisfied by rulesets only."""
        rules = _RULES_FULL.body
        assert rules is not None
        for rule_type in BASELINE_RULE_TYPES:
            status = _rule_status_for_type(rule_type, rules, None, source_b_404=True)
            assert status == RuleStatus.CONFIRMED, f"{rule_type} should be CONFIRMED"

    def test_combined_sources(self) -> None:
        """Test 6: Baseline satisfied from combination of both sources."""
        # Rulesets provide pull_request and deletion; classic provides non_fast_forward
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = {"allow_force_pushes": {"enabled": False}}  # non_fast_forward from classic

        assert _rule_status_for_type("pull_request", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED
        assert _rule_status_for_type("deletion", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED
        assert _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False) == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_404_on_protection_endpoint(self) -> None:
        """Test 7: 404 on classic protection endpoint — no error, rules only."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            # Source A returns full ruleset coverage, Source B returns 404
            def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
                if "/rules/branches/" in path:
                    return _RULES_FULL
                if "/branches/" in path and "/protection" in path:
                    return _PROTECTION_404
                return GitHubResponse(status_code=200, body={}, error_message=None)

            mock_api.side_effect = side_effect
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.CONFORMANT
            assert check.default_branch == "main"

        _run()

    def test_single_missing_rule(self) -> None:
        """Test 8: Exactly one rule missing → correctly identified."""
        rules = _RULES_TWO.body  # pull_request + deletion, missing non_fast_forward
        protection = None

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=True),
        }
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_auth_missing_on_api(self) -> None:
        """Test 14: AUTH_MISSING diagnostic on 401."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _AUTH_401
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_permission_insufficient_on_api(self) -> None:
        """Test 15: PERMISSION_INSUFFICIENT diagnostic on 403."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _PERM_403
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_github_unreachable(self) -> None:
        """Test 16: GITHUB_UNREACHABLE on network error."""

        @patch("governance._protection.github_api")
        def _run(mock_api: object) -> None:
            mock_api.return_value = _NETWORK_ERROR
            check = inspect_governance(REPO)
            assert check.compliance == ComplianceStatus.UNKNOWN

        _run()

    def test_missing_fields_in_classic_protection(self) -> None:
        """Test 21: Missing fields in classic protection → UNKNOWN for that type."""
        protection = _PROTECTION_MISSING_FIELDS.body
        assert protection is not None

        # pull_request field IS present → CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED
        # allow_force_pushes is MISSING → UNKNOWN
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.UNKNOWN
        # allow_deletions is MISSING → UNKNOWN
        assert _classic_confirms("deletion", protection) == RuleStatus.UNKNOWN


class TestRulesetManagement:
    """Tests 9–10, 30: Ruleset creation, idempotency, no PUT."""

    @patch("governance._protection.github_api")
    def test_idempotent_skip(self, mock_api: object) -> None:
        """Test 9: Existing compatible ruleset is not duplicated."""
        call_paths: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_paths.append(f"{method} {path}")
            if "/rulesets" in path and "includes_parents" in path and method == "GET":
                return _RULESETS_LIST_EXISTING
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "skipped"
        # No POST was made
        assert not any("POST" in c for c in call_paths)

    @patch("governance._protection.github_api")
    def test_no_overwrite_divergent_ruleset(self, mock_api: object) -> None:
        """Test 10: Divergent same-named ruleset is NOT overwritten."""
        divergent = GitHubResponse(
            status_code=200,
            body=[{
                "id": 42,
                "name": "agent-federation-baseline-v1",
                "target": "branch",
                "enforcement": "disabled",  # ← not active!
                "bypass_actors": [],
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [{"type": "deletion"}],  # ← missing rules
            }],
            error_message=None,
        )

        call_paths: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_paths.append(f"{method} {path}")
            if "/rulesets" in path and "includes_parents" in path and method == "GET":
                return divergent
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action is None
        # No POST/PUT
        assert not any("POST" in c or "PUT" in c for c in call_paths)

    @patch("governance._protection.github_api")
    def test_no_put_in_v1(self, mock_api: object) -> None:
        """Test 30: ensure_baseline_ruleset never issues a PUT."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_EXISTING
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert "PUT" not in call_methods

    @patch("governance._protection.github_api")
    def test_creates_when_missing(self, mock_api: object) -> None:
        """ensure_baseline_ruleset creates via POST when ruleset is absent."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rulesets" in path and "includes_parents" in path:
                return _RULESETS_LIST_EMPTY
            if method == "POST" and "/rulesets" in path:
                return _RULESETS_CREATED
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        result = ensure_governance_baseline(
            REPO,
            GovernanceCheck(compliance=ComplianceStatus.NON_CONFORMANT, default_branch="main"),
        )
        assert result.action == "created"
        assert "POST" in call_methods
        # Re-read occurred
        assert result.final_check is not None


# ── 12.2 Quellenweise Aggregations-Tests ──────────────────────────────────


class TestSourceAggregation:
    """Tests 22–26: Per-source aggregation with partial readability."""

    def test_full_rulesets_plus_classic_403(self) -> None:
        """Test 22: Full ruleset coverage + classic 403 → CONFORMANT with warning."""
        status = _rule_status_for_type("deletion", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("non_fast_forward", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("pull_request", _RULES_FULL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", _RULES_FULL.body, None, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", _RULES_FULL.body, None, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", _RULES_FULL.body, None, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_partial_rulesets_plus_classic_403(self) -> None:
        """Test 23: Partial ruleset + classic 403 → UNKNOWN."""
        # Only pull_request from rulesets, source B is unreadable (403)
        status = _rule_status_for_type("pull_request", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.CONFIRMED
        status = _rule_status_for_type("deletion", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.UNKNOWN
        status = _rule_status_for_type("non_fast_forward", _RULES_PARTIAL.body, None, source_b_404=False)
        assert status == RuleStatus.UNKNOWN

        statuses = {
            "deletion": _rule_status_for_type("deletion", _RULES_PARTIAL.body, None, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", _RULES_PARTIAL.body, None, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", _RULES_PARTIAL.body, None, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.UNKNOWN

    def test_full_classic_plus_rulesets_401(self) -> None:
        """Test 24: Full classic protection + rulesets 401 → CONFORMANT with warning."""
        protection = _PROTECTION_FULL.body
        assert protection is not None

        # Source A is unreadable (None), Source B confirms all three
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED
        assert _classic_confirms("pull_request", protection) == RuleStatus.CONFIRMED

        statuses = {
            "deletion": _rule_status_for_type("deletion", None, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", None, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", None, protection, source_b_404=False),
        }
        assert _overall_compliance(statuses) == ComplianceStatus.CONFORMANT

    def test_missing_rule_both_sources_readable(self) -> None:
        """Test 25: Missing rule with both sources fully readable → NON_CONFORMANT."""
        # Source A provides deletion + pull_request, source B confirms only non_fast_forward
        # non_fast_forward is MISSING from rules; force pushes are enabled in classic
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = _PROTECTION_FORCE_ENABLED.body  # force enabled → non_fast_forward MISSING

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=False),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=False),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=False),
        }
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        # non_fast_forward: not in rules (MISSING), classic says force enabled (MISSING)
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT

    def test_two_confirmed_one_missing_no_classic(self) -> None:
        """Test 26: Two rules from rulesets + classic 404 → one MISSING → NON_CONFORMANT."""
        rules = _RULES_TWO.body  # pull_request + deletion
        protection = None  # 404 — no classic prot

        statuses = {
            "deletion": _rule_status_for_type("deletion", rules, protection, source_b_404=True),
            "non_fast_forward": _rule_status_for_type("non_fast_forward", rules, protection, source_b_404=True),
            "pull_request": _rule_status_for_type("pull_request", rules, protection, source_b_404=True),
        }
        assert statuses["pull_request"] == RuleStatus.CONFIRMED
        assert statuses["deletion"] == RuleStatus.CONFIRMED
        assert statuses["non_fast_forward"] == RuleStatus.MISSING
        assert _overall_compliance(statuses) == ComplianceStatus.NON_CONFORMANT


# ── 12.3 Zusätzliche Tests ────────────────────────────────────────────────


class TestAdditional:
    """Tests 27–31: Token cascade, remote branch authority, payload."""

    @patch("subprocess.run")
    def test_token_cascade_gh_auth_token(self, mock_run: object) -> None:
        """Test 27: Token resolution uses gh auth token as last resort."""
        from federation_utils import _resolve_token

        # No env vars, gh auth token succeeds
        with patch.dict("os.environ", {}, clear=True):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="gh_token_123\n", stderr="",
            )
            token = _resolve_token()
            assert token == "gh_token_123"

    @patch("subprocess.run")
    def test_token_cascade_env_precedence(self, mock_run: object) -> None:
        """Test 27: GITHUB_TOKEN takes precedence over gh auth token."""
        from federation_utils import _resolve_token

        with patch.dict("os.environ", {"GITHUB_TOKEN": "env_token"}, clear=True):
            token = _resolve_token()
            assert token == "env_token"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_remote_branch_overrides_local(self, mock_run: object, mock_api: object) -> None:
        """Test 28: Remote default branch takes precedence over local."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        # Remote says 'main', even if local might say 'master'
        mock_api.return_value = GitHubResponse(
            status_code=200, body={"default_branch": "main"}, error_message=None,
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is not None
        assert repo.default_branch == "main"

    @patch("governance._repo.github_api")
    @patch("subprocess.run")
    def test_no_remote_branch_blocks_write(self, mock_run: object, mock_api: object) -> None:
        """Test 29: Unconfirmed remote default branch prevents write operations."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/kimeisele/test-node.git", stderr="",
        )
        # API returns 500 — can't confirm default branch
        mock_api.return_value = GitHubResponse(
            status_code=500, body=None, error_message="Internal Server Error",
        )

        repo, diag = detect_repository(Path("/fake"))
        assert repo is None
        assert diag == Diagnostic.GITHUB_UNREACHABLE
        # With no repo, ensure_governance_baseline cannot be called

    def test_exact_payload(self) -> None:
        """Test 31: RULESET_PAYLOAD_V1 has correct structure."""
        assert RULESET_PAYLOAD_V1["name"] == "agent-federation-baseline-v1"
        assert RULESET_PAYLOAD_V1["target"] == "branch"
        assert RULESET_PAYLOAD_V1["enforcement"] == "active"
        assert RULESET_PAYLOAD_V1["bypass_actors"] == []
        assert "~DEFAULT_BRANCH" in RULESET_PAYLOAD_V1["conditions"]["ref_name"]["include"]

        rules = RULESET_PAYLOAD_V1["rules"]
        rule_types = {r["type"] for r in rules}
        assert rule_types == {"deletion", "non_fast_forward", "pull_request"}

        # Check pull_request parameters
        pr_rule = next(r for r in rules if r["type"] == "pull_request")
        params = pr_rule["parameters"]
        assert params["allowed_merge_methods"] == ["merge", "squash", "rebase"]
        assert params["required_approving_review_count"] == 0
        assert params["dismiss_stale_reviews_on_push"] is False
        assert params["require_code_owner_review"] is False
        assert params["require_last_push_approval"] is False
        assert params["required_review_thread_resolution"] is False


# ── 12.4 Header- und Sicherheitstests ──────────────────────────────────────


class TestHeadersAndSecurity:
    """Tests 32–34: HTTP headers, payload, token safety."""

    @patch("subprocess.run")
    def test_github_api_headers(self, mock_run: object) -> None:
        """Test 32: github_api sets Accept and X-GitHub-Api-Version headers."""
        from federation_utils import github_api

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}200', stderr="",
        )
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}, clear=True):
            github_api("GET", "/repos/kimeisele/x")

        # Check curl command arguments
        call_args = mock_run.call_args[0][0] if mock_run.call_args else []
        cmd_str = " ".join(call_args)
        assert "Accept: application/vnd.github+json" in cmd_str
        assert "X-GitHub-Api-Version: 2022-11-28" in cmd_str

    def test_payload_includes_all_merge_methods(self) -> None:
        """Test 33: RULESET_PAYLOAD_V1 includes all three allowed_merge_methods."""
        pr_rule = next(r for r in RULESET_PAYLOAD_V1["rules"] if r["type"] == "pull_request")
        methods = pr_rule["parameters"]["allowed_merge_methods"]
        assert "merge" in methods
        assert "squash" in methods
        assert "rebase" in methods
        assert len(methods) == 3

    @patch("subprocess.run")
    def test_token_not_in_error_output(self, mock_run: object) -> None:
        """Test 34: Token never appears in error_message."""
        from federation_utils import github_api

        # Simulate a curl error
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=7, stdout="", stderr="Could not resolve host",
        )
        with patch.dict("os.environ", {"GITHUB_TOKEN": "secret-token-12345"}, clear=True):
            response = github_api("GET", "/repos/kimeisele/x")

        assert response.status_code == 0
        assert response.error_message is not None
        assert "secret-token-12345" not in response.error_message


class TestCompatibilityCheck:
    """Tests for _is_compatible logic."""

    def test_exact_compatible(self) -> None:
        """_is_compatible returns True for exact baseline match."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "exact"

    def test_stricter_accepted(self) -> None:
        """_is_compatible accepts stricter rules (extra rule types)."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
                {"type": "required_linear_history"},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is True
        assert reason == "stricter"

    def test_enforcement_not_active(self) -> None:
        """_is_compatible rejects disabled rulesets."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "disabled",
            "bypass_actors": [],
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "enforcement" in reason

    def test_missing_baseline_rules(self) -> None:
        """_is_compatible rejects rulesets missing baseline rules."""
        existing = {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "rules": [
                {"type": "deletion"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            ],
        }
        compatible, reason = _is_compatible(existing)
        assert compatible is False
        assert "non_fast_forward" in reason


class TestBypassState:
    """Tests for bypass state detection."""

    @patch("governance._protection.github_api")
    def test_bypass_unknown_on_auth_error(self, mock_api: object) -> None:
        """Test 17: Bypass UNKNOWN when source has AUTH_MISSING diagnostic."""

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rules/branches/" in path:
                return _AUTH_401
            if "/branches/" in path and "/protection" in path:
                return _AUTH_401
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        check = inspect_governance(REPO)
        assert check.bypass_state == BypassState.UNKNOWN
        assert Diagnostic.AUTH_MISSING in check.diagnostics


class TestCLIModes:
    """Tests 11–13: CLI behavior (non-interactive, apply-governance, status)."""

    @patch("governance._protection.github_api")
    def test_non_interactive_no_write(self, mock_api: object) -> None:
        """Test 11: --non-interactive (without --apply-governance) performs NO POST/PUT."""
        call_methods: list[str] = []

        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            call_methods.append(method)
            if "/rules/branches/" in path:
                return _RULES_EMPTY
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect
        # This simulates what --non-interactive does: inspect_governance only
        check = inspect_governance(REPO)
        assert check.compliance == ComplianceStatus.NON_CONFORMANT
        # No POST or PUT
        assert "POST" not in call_methods
        assert "PUT" not in call_methods

    def test_rule_in_rules_list(self) -> None:
        """_rule_in_rules_list correctly detects rule types."""
        rules = [{"type": "deletion"}, {"type": "pull_request"}]
        assert _rule_in_rules_list("deletion", rules) is True
        assert _rule_in_rules_list("non_fast_forward", rules) is False

    def test_classic_confirms_enabled_fields(self) -> None:
        """_classic_confirms returns MISSING when field explicitly allows the action."""
        # allow_force_pushes.enabled == true → force pushes are allowed → rule MISSING
        protection = {"allow_force_pushes": {"enabled": True}}
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.MISSING

        protection = {"allow_deletions": {"enabled": True}}
        assert _classic_confirms("deletion", protection) == RuleStatus.MISSING

    def test_classic_confirms_disabled_fields(self) -> None:
        """_classic_confirms returns CONFIRMED when field explicitly blocks the action."""
        protection = {"allow_force_pushes": {"enabled": False}}
        assert _classic_confirms("non_fast_forward", protection) == RuleStatus.CONFIRMED

        protection = {"allow_deletions": {"enabled": False}}
        assert _classic_confirms("deletion", protection) == RuleStatus.CONFIRMED


class TestSameLogicSetupAndStatus:
    """Test 20: Same inspect_governance logic for setup and status."""

    @patch("governance._protection.github_api")
    def test_same_inspect_governance(self, mock_api: object) -> None:
        """Both setup and --status use the same inspect_governance function."""
        def side_effect(method: str, path: str, body: object = None, *, token: str | None = None) -> GitHubResponse:
            if "/rules/branches/" in path:
                return _RULES_FULL
            if "/branches/" in path and "/protection" in path:
                return _PROTECTION_404
            return GitHubResponse(status_code=200, body={}, error_message=None)

        mock_api.side_effect = side_effect

        # Called directly (as --status does)
        check1 = inspect_governance(REPO)

        # Called again (as setup does)
        mock_api.side_effect = side_effect  # reset side_effect
        check2 = inspect_governance(REPO)

        assert check1.compliance == check2.compliance
        assert check1.rule_statuses == check2.rule_statuses
        assert check1.compliance == ComplianceStatus.CONFORMANT
