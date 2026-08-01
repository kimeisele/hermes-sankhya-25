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
import urllib.request
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
        if not (0 <= self.max_new <= 50):
            raise ValueError("global_discovery_max_new must be within 0..50")
        if not (0 <= self.max_top_day <= 25):
            raise ValueError("global_discovery_max_top_day must be within 0..25")
        if not (0 <= self.max_comments_day <= 25):
            raise ValueError("global_discovery_max_comments_day must be within 0..25")
        if not (1 <= self.candidate_cap <= 20):
            raise ValueError("global_discovery_candidate_cap must be within 1..20")
        if not (1 <= self.excerpt_length <= 500):
            raise ValueError("global_discovery_excerpt_length must be within 1..500")
        for t in self.strong_terms + self.secondary_terms:
            if not isinstance(t, str) or not t.strip():
                raise ValueError("discovery terms must be non-empty strings")
        for h in self.internal_handles:
            if not isinstance(h, str) or not h.strip():
                raise ValueError("discovery internal handles must be non-empty strings")


# ---------------------------------------------------------------------------
# GET-only discovery client
# ---------------------------------------------------------------------------


class DiscoveryClient:
    """GET-only client for the three predefined Moltbook listing paths.

    Strict allowlist: only the exact new/top-day/comments-day listing
    forms may be requested, each with a hard maximum limit.  Redirects
    are rejected (no following to other hosts).  Over-returned listings
    are truncated to the requested limit.
    """

    # Production base URL is fixed.
    PRODUCTION_BASE_URL = "https://www.moltbook.com/api/v1"

    # Strict structured allowlist: sort -> (extra query, hard max limit)
    _LISTING_SPECS = {
        "new": ("", 50),
        "top": ("&time=day", 25),
        "comments": ("&time=day", 25),
    }

    def __init__(self, base_url: str = PRODUCTION_BASE_URL,
                 timeout: float = 30.0,
                 transport: Callable[..., dict[str, Any]] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def fetch_listing(self, sort: str, limit: int) -> list[dict[str, Any]]:
        """Fetch one predefined listing with a hard cap. Returns validated posts."""
        if sort not in self._LISTING_SPECS:
            raise ValueError(f"unknown listing sort: {sort}")
        _, hard_max = self._LISTING_SPECS[sort]
        if limit > hard_max:
            raise ValueError(f"limit {limit} exceeds hard maximum {hard_max} for {sort}")
        extra, _ = self._LISTING_SPECS[sort]
        path = f"/posts?sort={sort}{extra}&limit={limit}"
        raw = self._api_call("GET", path)
        posts = _parse_listing(raw)
        # Hard-cap over-returned listings to the requested limit.
        return posts[:limit]

    # -- transport --------------------------------------------------------

    def _api_call(self, method: str, path: str) -> dict[str, Any]:
        if method != "GET":
            raise RuntimeError(f"DiscoveryClient only supports GET, got {method}")
        # Path must exactly match one of the three allowed listing forms.
        if not self._is_allowed_path(path):
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
            # Only the production host may be contacted, and redirects are
            # rejected (no following to any other host).
            import urllib.parse
            allowed_netloc = urllib.parse.urlparse(self.PRODUCTION_BASE_URL).netloc
            actual_netloc = urllib.parse.urlparse(url).netloc
            if actual_netloc != allowed_netloc:
                raise RuntimeError(f"DiscoveryClient host not allowed: {actual_netloc}")
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API {method} {path} → {exc.code}: {err}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API unreachable {method} {path}: {exc}") from exc
        except TimeoutError:
            raise RuntimeError(f"API timeout {method} {path} after {self.timeout}s")

    @staticmethod
    def _is_allowed_path(path: str) -> bool:
        for sort, (extra, hard_max) in DiscoveryClient._LISTING_SPECS.items():
            if extra:
                for limit in range(0, hard_max + 1):
                    if path == f"/posts?sort={sort}{extra}&limit={limit}":
                        return True
            else:
                for limit in range(0, hard_max + 1):
                    if path == f"/posts?sort={sort}&limit={limit}":
                        return True
        return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any HTTP redirect instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(f"redirect not allowed: {code} → {newurl}")


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


def _term_in(text_norm: str, term: str) -> bool:
    """Word/phrase-boundary match for a normalized term.

    Single-word terms match only at word boundaries; multi-word phrases
    match as exact normalized substrings between word boundaries.
    """
    term_norm = _normalize(term).strip()
    if not term_norm:
        return False
    if " " in term_norm:
        return f" {term_norm} " in f" {text_norm} "
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])", text_norm))


def score_post(title: str, content: str, strong_terms: list[str],
               secondary_terms: list[str],
               has_agent_context: bool) -> tuple[int, list[str]]:
    """Deterministic relevance score and matched terms.

    Qualification: at least one strong term, OR at least two distinct
    secondary terms plus unambiguous agent/task/tool context.
    Returns (score, matched_terms); a zero score means not qualified.
    """
    hay = _normalize(f"{title}\n{content}")
    strong_hits = [t for t in strong_terms if _term_in(hay, t)]
    secondary_hits = [t for t in secondary_terms if _term_in(hay, t)]

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
# Issue-notification sanitization (untrusted external values)
# ---------------------------------------------------------------------------


def sanitize_text(value: str, max_length: int) -> str:
    """Neutralize untrusted external text for issue comments.

    Strips control characters and newlines, removes Markdown special
    characters, neutralizes ``@`` (no account mentions), and truncates.
    """
    if not isinstance(value, str):
        value = ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"[*_`\[\]()#<>~|\\{}]", "", value)
    value = value.replace("@", "")
    value = re.sub(r"\s+", " ", value)
    return value[:max_length].strip()


_MARKER_PREFIX = "<!-- hermes-global-discovery-v1:candidate-ids="
_MARKER_SUFFIX = " -->"


def build_marker(ids: list[str]) -> str:
    """Build the machine-readable dedup marker with sorted UUIDs."""
    return f"{_MARKER_PREFIX}{','.join(sorted(ids))}{_MARKER_SUFFIX}"


def parse_marker_ids(text: str) -> set[str]:
    """Extract already-reported candidate IDs from a marker in a comment body."""
    if not isinstance(text, str):
        return set()
    start = text.find(_MARKER_PREFIX)
    if start < 0:
        return set()
    body = text[start + len(_MARKER_PREFIX):]
    end = body.find(_MARKER_SUFFIX)
    if end < 0:
        return set()
    raw = body[:end]
    return {i for i in raw.split(",") if i}


def new_candidate_ids(candidates: list[Candidate],
                      reported_ids: set[str]) -> list[str]:
    """Candidate IDs never reported before, in deterministic order."""
    seen = set(reported_ids)
    return [c.post_id for c in candidates if c.post_id not in seen]


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

        # Dedup, filter; process at most 100 unique post objects before
        # the relevance check.
        candidates_pool: list[Candidate] = []
        processed = 0
        for pid, p in by_id.items():
            if processed >= 100:
                break
            processed += 1
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
    """Unambiguous agent/task/tool context signals (word-bounded)."""
    hay = _normalize(f"{title}\n{content}")
    signals = (
        "agent", "task", "tool", "runner", "pipeline", "workflow",
        "automation", "deploy", "bot", "llm", "subagent", "model",
    )
    return any(_term_in(hay, s) for s in signals)


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
