"""Shared utilities for federation scripts."""
from __future__ import annotations

import json
import os
import subprocess


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
