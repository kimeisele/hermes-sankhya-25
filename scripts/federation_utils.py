"""Shared utilities for federation scripts."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

# ── Structured GitHub API access ───────────────────────────────────────────

_API_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_API_BASE = "https://api.github.com"


@dataclass
class GitHubResponse:
    """Structured result of a GitHub API call.

    *status_code* is 0 for network / timeout errors where no HTTP
    response was received.
    """

    status_code: int
    body: dict[str, Any] | list[dict[str, Any]] | None
    error_message: str | None


def _resolve_token(token: str | None = None) -> str | None:
    """Resolve a GitHub token from the canonical cascade.

    1. explicit *token* parameter
    2. ``GITHUB_TOKEN`` environment variable
    3. ``GH_TOKEN`` environment variable
    4. ``gh auth token`` (subprocess, ignored on failure)
    """
    if token:
        return token
    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, FileNotFoundError):
        pass
    return None


def github_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
) -> GitHubResponse:
    """Make a GitHub REST API call via curl.

    *method*  — HTTP method (``GET``, ``POST``, …).
    *path*    — API path relative to ``https://api.github.com``,
                e.g. ``/repos/kimeisele/x/branches/main/protection``.
    *body*    — optional JSON request body.
    *token*   — optional explicit token; if ``None`` the canonical
                cascade is used (see :func:`_resolve_token`).

    Returns a :class:`GitHubResponse` with:

    * *status_code* — HTTP status (0 for network / timeout errors).
    * *body* — decoded JSON, or ``None`` on failure.
    * *error_message* — GitHub error message or curl / system error,
      ``None`` on success.
    """
    resolved = _resolve_token(token)
    cmd = [
        "curl", "-s", "-w", "%{http_code}",
        "--connect-timeout", "10",
        "-H", f"Accept: {_API_ACCEPT}",
        "-H", f"X-GitHub-Api-Version: {_API_VERSION}",
    ]
    if resolved:
        cmd += ["-H", f"Authorization: token {resolved}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd += ["-X", method, f"{_API_BASE}{path}"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message=f"curl error: {result.stderr.strip() or 'exit code ' + str(result.returncode)}",
        )

    stdout = result.stdout
    if len(stdout) < 3:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message="empty or truncated curl response",
        )

    # curl -w "%{http_code}" appends the status to stdout
    status_str = stdout[-3:]
    response_body = stdout[:-3]

    try:
        status_code = int(status_str)
    except ValueError:
        return GitHubResponse(
            status_code=0,
            body=None,
            error_message=f"could not parse HTTP status from curl output: {status_str!r}",
        )

    if not response_body.strip():
        return GitHubResponse(
            status_code=status_code,
            body=None,
            error_message=None if 200 <= status_code < 300 else f"HTTP {status_code}: empty response",
        )

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        return GitHubResponse(
            status_code=status_code,
            body=None,
            error_message=f"invalid JSON response: {exc}",
        )

    if 200 <= status_code < 300:
        return GitHubResponse(status_code=status_code, body=parsed, error_message=None)

    # GitHub error response — extract message
    if isinstance(parsed, dict):
        msg = parsed.get("message", f"HTTP {status_code}")
    else:
        msg = f"HTTP {status_code}"
    return GitHubResponse(status_code=status_code, body=parsed, error_message=str(msg))


# ── Legacy helpers (unchanged) ─────────────────────────────────────────────


def curl_json(url: str, token: str | None = None) -> dict | list | None:
    """Fetch JSON from *url* using curl.  Returns None on failure."""
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["curl", "-sf", "--connect-timeout", "10", "-H", "Accept: application/json"]
    if token:
        cmd += ["-H", f"Authorization: token {token}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def curl_bytes(url: str, token: str | None = None) -> bytes | None:
    """Fetch raw bytes from *url* using curl.  Returns None on failure."""
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["curl", "-sfL", "--connect-timeout", "10"]
    if token:
        cmd += ["-H", f"Authorization: token {token}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout


def display_name(repo_name: str) -> str:
    """Convert a repo name like 'my-cool-node' to 'My Cool Node'."""
    return " ".join(word.capitalize() for word in repo_name.replace("_", "-").split("-") if word) or repo_name
