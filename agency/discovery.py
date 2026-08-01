"""Bounded Moltbook global discovery — GET-only, deterministic, no models.

Sweeps three predefined global post listings (new / top-day / comments-day),
deduplicates by post ID, excludes internal handles and already-known
evidence IDs, applies a deterministic relevance filter, and emits at most
``global_discovery_candidate_cap`` candidates.

No models, no writes, no search dependency, no generic URLs.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_MOLTBOOK_POST_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryConfig:
    enabled: bool = True
    max_new: int = 50
    max_top_day: int = 25
    max_comments_day: int = 25
    candidate_cap: int = 20
    excerpt_length: int = 500
    strong_terms: list[str] = field(default_factory=list)
    secondary_terms: list[str] = field(default_factory=list)
    internal_handles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "DiscoveryConfig":
        d = cfg.get("global_discovery", {})
        return cls(
            enabled=bool(d.get("global_discovery_enabled", True)),
            max_new=int(d.get("global_discovery_max_new", 50)),
            max_top_day=int(d.get("global_discovery_max_top_day", 25)),
            max_comments_day=int(d.get("global_discovery_max_comments_day", 25)),
            candidate_cap=int(d.get("global_discovery_candidate_cap", 20)),
            excerpt_length=int(d.get("global_discovery_excerpt_length", 500)),
            strong_terms=list(d.get("global_discovery_strong_terms", [])),
            secondary_terms=list(d.get("global_discovery_secondary_terms", [])),
            internal_handles=list(d.get("global_discovery_internal_handles", [])),
        )

    def validate(self) -> None:
        if self.max_new < 0 or self.max_top_day < 0 or self.max_comments_day < 0:
            raise ValueError("discovery listing limits must be non-negative")
        if self.candidate_cap <= 0:
            raise ValueError("global_discovery_candidate_cap must be positive")
        if self.excerpt_length <= 0:
            raise ValueError("global_discovery_excerpt_length must be positive")


# ---------------------------------------------------------------------------
# GET-only discovery client
# ---------------------------------------------------------------------------


class DiscoveryClient:
    """GET-only client for predefined Moltbook global listing paths."""

    # Allowed paths — no generic URLs, no non-GET methods.
    _ALLOWED_PATHS = (
        "/posts?sort=new",
        "/posts?sort=top&time=day",
        "/posts?sort=comments&time=day",
    )

    def __init__(self, base_url: str = "https://www.moltbook.com/api/v1",
                 timeout: float = 30.0,
                 transport: Callable[..., dict[str, Any]] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def fetch_listing(self, sort: str, limit: int) -> list[dict[str, Any]]:
        """Fetch one predefined listing. Returns a validated list of posts."""
        if sort not in ("new", "top", "comments"):
            raise ValueError(f"unknown listing sort: {sort}")
        query = f"sort={sort}"
        if sort in ("top", "comments"):
            query += "&time=day"
        query += f"&limit={limit}"
        path = f"/posts?{query}"
        raw = self._api_call("GET", path)
        return _parse_listing(raw)

    # -- transport --------------------------------------------------------

    def _api_call(self, method: str, path: str) -> dict[str, Any]:
        if method != "GET":
            raise RuntimeError(f"DiscoveryClient only supports GET, got {method}")
        if not path.startswith("/posts?"):
            raise RuntimeError(f"DiscoveryClient path not allowed: {path}")

        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            if self._transport is not None:
                resp = self._transport({"url": url, "method": "GET", "path": path})
                if isinstance(resp, dict) and "error" in resp and not any(
                        k in resp for k in ("posts", "success")):
                    raise RuntimeError(f"API {method} {path} → {resp['error']}")
                return resp
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API {method} {path} → {exc.code}: {err}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API unreachable {method} {path}: {exc}") from exc
        except TimeoutError:
            raise RuntimeError(f"API timeout {method} {path} after {self.timeout}s")


def _parse_listing(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the listing shape; unknown shapes fail closed."""
    if not isinstance(raw, dict):
        raise ValueError("listing response is not an object")
    posts = raw.get("posts")
    if not isinstance(posts, list):
        raise ValueError("listing response missing 'posts' list")
    validated: list[dict[str, Any]] = []
    for p in posts:
        if not isinstance(p, dict):
            raise ValueError("listing post is not an object")
        pid = p.get("id", "")
        if not isinstance(pid, str) or not _MOLTBOOK_POST_RE.fullmatch(pid):
            raise ValueError(f"listing post has malformed id: {pid!r}")
        validated.append(p)
    return validated


# ---------------------------------------------------------------------------
# Deterministic relevance
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    post_id: str
    canonical_url: str
    author_handle: str
    title: str
    created_at: str
    content_excerpt: str
    content_sha256: str
    matched_terms: list[str]
    relevance_score: int
    listing_sources: list[str]
    already_known: bool = False


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def score_post(title: str, content: str, strong_terms: list[str],
               secondary_terms: list[str],
               has_agent_context: bool) -> tuple[int, list[str]]:
    """Deterministic relevance score and matched terms.

    Qualification: at least one strong term, OR at least two distinct
    secondary terms plus unambiguous agent/task/tool context.
    Returns (score, matched_terms); a zero score means not qualified.
    """
    hay = _normalize(f"{title}\n{content}")
    strong_hits = [t for t in strong_terms if t in hay]
    secondary_hits = [t for t in secondary_terms if t in hay]

    if strong_hits:
        score = 10 + len(strong_hits) * 2
        return score, strong_hits + secondary_hits

    distinct_secondary = sorted(set(secondary_hits))
    if len(distinct_secondary) >= 2 and has_agent_context:
        score = 5 + len(distinct_secondary)
        return score, distinct_secondary

    return 0, []


def _excerpt(content: str, length: int) -> str:
    return content[:length]


def _content_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bounded discovery run
# ---------------------------------------------------------------------------


class GlobalDiscovery:
    """Bounded read-only discovery sweep over the three global listings."""

    def __init__(self, client: DiscoveryClient, config: DiscoveryConfig,
                 known_ids: set[str] | None = None) -> None:
        self._client = client
        self._cfg = config
        self._known_ids = known_ids or set()
        self._internal = set(config.internal_handles)

    def run(self) -> list[Candidate]:
        self._cfg.validate()
        if not self._cfg.enabled:
            return []

        by_id: dict[str, dict[str, Any]] = {}
        listing_src: dict[str, list[str]] = {}

        # Bounded fetches (max 50 new, 25 top-day, 25 comments-day)
        specs = [("new", self._cfg.max_new),
                 ("top", self._cfg.max_top_day),
                 ("comments", self._cfg.max_comments_day)]
        for sort, limit in specs:
            if limit <= 0:
                continue
            posts = self._client.fetch_listing(sort, limit)
            for p in posts:
                pid = p["id"]
                if pid not in by_id:
                    by_id[pid] = p
                    listing_src[pid] = []
                listing_src[pid].append(sort)

        # Dedup, filter, cap at 100 objects before relevance
        candidates_pool: list[Candidate] = []
        for pid, p in by_id.items():
            if len(candidates_pool) >= 100:
                break
            if p.get("is_deleted") or p.get("is_spam"):
                continue
            author = _author_name(p)
            if author in self._internal:
                continue
            if pid in self._known_ids:
                continue
            title = str(p.get("title", ""))
            content = str(p.get("content", ""))
            created_at = str(p.get("created_at", ""))
            has_agent_context = _has_agent_context(title, content)
            score, matched = score_post(
                title, content, self._cfg.strong_terms,
                self._cfg.secondary_terms, has_agent_context)
            if score <= 0:
                continue
            candidates_pool.append(Candidate(
                post_id=pid,
                canonical_url=f"https://www.moltbook.com/post/{pid}",
                author_handle=author,
                title=title,
                created_at=created_at,
                content_excerpt=_excerpt(content, self._cfg.excerpt_length),
                content_sha256=_content_sha(content),
                matched_terms=matched,
                relevance_score=score,
                listing_sources=sorted(set(listing_src[pid])),
                already_known=False,
            ))

        # Deterministic sort: relevance desc, created_at desc, post_id asc
        candidates_pool.sort(key=lambda c: (
            -c.relevance_score, -_iso_ordinal(c.created_at), c.post_id))
        return candidates_pool[: self._cfg.candidate_cap]


def _author_name(post: dict[str, Any]) -> str:
    author = post.get("author")
    if isinstance(author, dict):
        return str(author.get("name", "unknown"))
    return str(post.get("author_handle", "unknown"))


def _has_agent_context(title: str, content: str) -> bool:
    """Unambiguous agent/task/tool context signals."""
    hay = _normalize(f"{title}\n{content}")
    signals = (
        "agent", "task", "tool", "runner", "pipeline", "workflow",
        "automation", "deploy", "bot", "llm", "subagent", "model",
    )
    return any(s in hay for s in signals)


def _iso_key(created_at: str) -> str:
    """Created-at ordering key; malformed values sort last."""
    if not created_at:
        return "9999"
    return created_at


def _iso_ordinal(created_at: str) -> float:
    """Numeric ordering key for created_at (desc). Malformed → 0 (sorts last)."""
    import datetime as _dt
    try:
        dt = _dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Artifact rendering
# ---------------------------------------------------------------------------


def render_report(candidates: list[Candidate], cfg: DiscoveryConfig,
                  model_calls: int = 0, tokens: int = 0,
                  external_writes: int = 0) -> str:
    lines = [
        "# Moltbook Global Discovery — Report",
        "",
        "- Status: completed",
        f"- Candidate count: {len(candidates)}",
        f"- Model calls: {model_calls}",
        f"- Tokens: {tokens}",
        f"- External writes: {external_writes}",
        "",
    ]
    if not candidates:
        lines.append("No candidates matched the deterministic relevance filter.")
        return "\n".join(lines) + "\n"
    lines.append("## Candidates")
    lines.append("")
    for i, c in enumerate(candidates, 1):
        lines.append(f"### {i}. {c.post_id}")
        lines.append(f"- Author: {c.author_handle}")
        lines.append(f"- Title: {c.title}")
        lines.append(f"- URL: {c.canonical_url}")
        lines.append(f"- Created: {c.created_at}")
        lines.append(f"- Score: {c.relevance_score}")
        lines.append(f"- Matched terms: {', '.join(c.matched_terms)}")
        lines.append(f"- Listings: {', '.join(c.listing_sources)}")
        lines.append(f"- Content SHA-256: {c.content_sha256}")
        lines.append("")
    return "\n".join(lines) + "\n"


def candidates_to_json(candidates: list[Candidate]) -> dict[str, Any]:
    return {
        "status": "completed",
        "candidate_count": len(candidates),
        "model_calls": 0,
        "tokens": 0,
        "external_writes": 0,
        "candidates": [
            {
                "post_id": c.post_id,
                "canonical_url": c.canonical_url,
                "author_handle": c.author_handle,
                "title": c.title,
                "created_at": c.created_at,
                "content_excerpt": c.content_excerpt,
                "content_sha256": c.content_sha256,
                "matched_terms": c.matched_terms,
                "relevance_score": c.relevance_score,
                "listing_sources": c.listing_sources,
                "already_known": c.already_known,
            }
            for c in candidates
        ],
    }
