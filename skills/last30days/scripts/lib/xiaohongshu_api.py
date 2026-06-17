"""Xiaohongshu (小红书) search client for last30days.

Talks to a locally-running xiaohongshu-mcp service over the Model Context
Protocol (MCP) Streamable HTTP transport (xpzouying/xiaohongshu-mcp):

- POST {base}/mcp  — JSON-RPC 2.0: initialize → notifications/initialized → tools/call
- GET  {base}/health

The current xiaohongshu-mcp ("纯查询精简版") exposes ONLY the MCP endpoint and a
health check — the older REST API (/api/v1/feeds/search) has been removed — so
this client speaks MCP directly. Tools used: check_login_status, search_feeds.

Output items match the shared web-item ("grounding") shape so normalize.py can
treat Xiaohongshu notes like any other web source.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import http

# MCP protocol version we advertise on initialize; the server may negotiate a
# different one back, which we then echo on subsequent requests.
MCP_PROTOCOL_VERSION = "2025-06-18"


def _to_int(value: Any) -> int:
    """Convert Xiaohongshu count strings to int.

    Supports plain ints and Chinese suffixes like 1.2万 / 3亿.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower().replace(",", "")
    if not text:
        return 0

    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10000)
        if text.endswith("亿"):
            return int(float(text[:-1]) * 100000000)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _timestamp_to_date_ms(ts: Any) -> Optional[str]:
    """Convert millisecond timestamp to YYYY-MM-DD."""
    try:
        iv = int(ts)
        if iv <= 0:
            return None
        # API examples use milliseconds.
        dt = datetime.fromtimestamp(iv / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _relevance_from_interactions(likes: int, comments: int, favorites: int) -> float:
    """Heuristic relevance score from engagement metrics."""
    # Weighted engagement with soft caps to [0, 1].
    weighted = (likes * 1.0) + (comments * 2.5) + (favorites * 1.5)
    # 5000 weighted engagement ~= strong relevance.
    score = min(1.0, max(0.05, weighted / 5000.0))
    return round(score, 3)


def _build_note_url(feed_id: str, xsec_token: str) -> str:
    """Build a stable Xiaohongshu note URL."""
    if xsec_token:
        return f"https://www.xiaohongshu.com/explore/{feed_id}?xsec_token={xsec_token}"
    return f"https://www.xiaohongshu.com/explore/{feed_id}"


# --------------------------------------------------------------------------- #
# Minimal MCP Streamable-HTTP client (stdlib only)
# --------------------------------------------------------------------------- #

class _MCPError(Exception):
    """An MCP-level error (JSON-RPC error, or tool isError)."""


def _parse_mcp_body(body: str) -> Dict[str, Any]:
    """Parse an MCP HTTP response body — plain JSON or SSE-framed.

    The server runs with JSONResponse=true so responses are normally plain
    application/json, but we also tolerate ``data: {...}`` SSE framing.
    """
    body = (body or "").strip()
    if not body:
        return {}
    if body.startswith("data:") or "\ndata:" in body or body.startswith("event:"):
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return {}
    return json.loads(body)


def _raw_post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int):
    """POST JSON to ``url``; return (body_text, session_id_header)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        session_id = resp.headers.get("Mcp-Session-Id")
        return body, session_id


class _MCPSession:
    """One MCP connection: initialize once, then call any number of tools."""

    def __init__(self, base: str, timeout: int = 20):
        self.endpoint = base.rstrip("/") + "/mcp"
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.protocol = MCP_PROTOCOL_VERSION
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": http.USER_AGENT,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol,
                "capabilities": {},
                "clientInfo": {"name": "last30days", "version": "3"},
            },
        }
        body, session_id = _raw_post(self.endpoint, payload, self._headers(), self.timeout)
        if session_id:
            self.session_id = session_id
        result = _parse_mcp_body(body)
        negotiated = (result.get("result") or {}).get("protocolVersion")
        if negotiated:
            self.protocol = negotiated
        # Required handshake step; notification returns 202/empty — ignore errors.
        try:
            _raw_post(
                self.endpoint,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                self._headers(),
                self.timeout,
            )
        except (urllib.error.URLError, OSError):
            pass

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        body, _ = _raw_post(self.endpoint, payload, self._headers(), self.timeout)
        resp = _parse_mcp_body(body)
        if "error" in resp:
            raise _MCPError(str(resp["error"]))
        return resp.get("result") or {}


def _tool_text(result: Dict[str, Any]) -> str:
    """Extract the first text content block from a tools/call result."""
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def check_login(base_url: str, timeout: int = 8) -> bool:
    """Return True if the xiaohongshu-mcp service is reachable AND logged in."""
    base = (base_url or "").rstrip("/")
    if not base:
        return False
    try:
        session = _MCPSession(base, timeout=timeout)
        session.initialize()
        text = _tool_text(session.call_tool("check_login_status", {}))
    except (urllib.error.URLError, OSError, _MCPError, json.JSONDecodeError):
        return False
    return ("已登录" in text or "✅" in text) and "未登录" not in text


def search_feeds(
    topic: str,
    from_date: str,
    to_date: str,
    base_url: str,
    depth: str = "default",
) -> List[Dict[str, Any]]:
    """Search Xiaohongshu via MCP and normalize to web-item shape."""
    base = (base_url or "").rstrip("/")
    if not base:
        raise ValueError("Missing Xiaohongshu API base URL")

    session = _MCPSession(base, timeout=30)
    session.initialize()

    # Login sanity check (MCP search requires an authenticated browser session).
    login_text = _tool_text(session.call_tool("check_login_status", {}))
    if "未登录" in login_text or ("已登录" not in login_text and "✅" not in login_text):
        raise http.HTTPError("Xiaohongshu MCP reachable but not logged in")

    # Tool supports filters; use recency-oriented defaults by depth.
    publish_time = "一天内" if depth == "quick" else "一周内" if depth == "default" else "半年内"
    result = session.call_tool(
        "search_feeds",
        {
            "keyword": topic,
            "filters": {
                "sort_by": "综合",
                "note_type": "不限",
                "publish_time": publish_time,
                "search_scope": "不限",
                "location": "不限",
            },
        },
    )
    if result.get("isError"):
        raise http.HTTPError("Xiaohongshu search failed: " + _tool_text(result)[:200])

    # search_feeds returns its payload as a JSON string in a text content block.
    text = _tool_text(result)
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {}
    feeds = payload.get("feeds") if isinstance(payload, dict) else []
    if not isinstance(feeds, list):
        feeds = []

    # Cap source volume similarly to other web sources.
    limit = {"quick": 8, "default": 15, "deep": 25}.get(depth, 15)
    items: List[Dict[str, Any]] = []

    for i, feed in enumerate(feeds[:limit]):
        if not isinstance(feed, dict):
            continue
        note = feed.get("noteCard") or {}
        if not isinstance(note, dict):
            note = {}
        interact = note.get("interactInfo") or {}
        if not isinstance(interact, dict):
            interact = {}

        feed_id = str(feed.get("id") or note.get("noteId") or "").strip()
        if not feed_id:
            continue

        xsec_token = str(feed.get("xsecToken") or note.get("xsecToken") or "").strip()
        title = str(note.get("displayTitle") or note.get("title") or "").strip()
        snippet = str(
            note.get("desc")
            or note.get("displayDesc")
            or title
            or ""
        ).strip()

        likes = _to_int(interact.get("likedCount"))
        comments = _to_int(interact.get("commentCount"))
        favorites = _to_int(interact.get("collectedCount"))

        # MCP search results carry no publish timestamp; date stays unknown
        # (normalize keeps dateless Xiaohongshu items — require_date is grounding-only).
        date_value = _timestamp_to_date_ms(note.get("time"))
        why = f"Xiaohongshu engagement: likes={likes}, comments={comments}, favorites={favorites}"

        items.append({
            "id": f"XHS{i+1}",
            "title": title[:200] if title else f"Xiaohongshu note {feed_id}",
            "url": _build_note_url(feed_id, xsec_token),
            "source_domain": "xiaohongshu.com",
            "snippet": snippet[:500],
            "date": date_value,
            "date_confidence": "high" if date_value else "low",
            "relevance": _relevance_from_interactions(likes, comments, favorites),
            "why_relevant": why,
            # Keep raw engagement for debugging/possible future rendering.
            "engagement": {
                "likes": likes,
                "comments": comments,
                "favorites": favorites,
            },
        })

    return items
