"""Bilibili search and enrichment via the TikHub API for /last30days.

Bilibili is China's long-form video community — the closest analogue to YouTube
in this skill, so its items are shaped to match the YouTube item dict and reuse
``normalize._normalize_youtube`` downstream (title + description + transcript +
top comments, with views/likes/danmaku engagement).

Backend: TikHub Bilibili-Web-API (https://api.tikhub.io), Bearer auth.
  - search:    GET /api/v1/bilibili/web/fetch_general_search
  - comments:  GET /api/v1/bilibili/web/fetch_video_comments
  - subtitle:  GET /api/v1/bilibili/web/fetch_video_subtitle
Requires TIKHUB_API_KEY in config. Pay-as-you-go (~$0.001/request).
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Set

from . import dates, http, log
from .relevance import token_overlap_relevance as _compute_relevance

TIKHUB_BASE = "https://api.tikhub.io/api/v1/bilibili/web"

# Depth configurations: how many results to fetch / subtitles to extract.
DEPTH_CONFIG = {
    "quick":   {"results": 10, "max_subtitles": 2, "pages": 1},
    "default": {"results": 20, "max_subtitles": 4, "pages": 1},
    "deep":    {"results": 40, "max_subtitles": 6, "pages": 2},
}

SUBTITLE_MAX_WORDS = 500

_EM_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _log(msg: str):
    log.source_log("Bilibili", msg)


def _clean_html(text: str) -> str:
    """Strip Bilibili search highlight tags (<em class="keyword">) and entities."""
    if not text:
        return ""
    text = _EM_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _to_int(value: Any) -> int:
    """Coerce a count to int. Handles ints, numeric strings, and 万/亿 suffixes."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
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


def _date_to_unix(date_str: str) -> Optional[int]:
    dt = dates.parse_date(date_str)
    if dt is None:
        return None
    try:
        return int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _unwrap(resp: Any) -> Any:
    """Unwrap TikHub's envelope to the raw Bilibili payload.

    TikHub returns {"code": 200, "data": {...}}. The Bilibili web search nests
    the real payload one or two levels deeper, so unwrap defensively.
    """
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data", resp)
    # Some TikHub endpoints double-wrap as data.data.
    if isinstance(data, dict) and "data" in data and "result" not in data:
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
    return data if isinstance(data, dict) else {}


def _extract_video_entries(payload: dict) -> List[Dict[str, Any]]:
    """Locate the list of video result dicts within a Bilibili search payload.

    The general-search response shapes seen in the wild:
      - {"result": [ {"result_type": "video", "data": [ ...videos... ]}, ... ]}
      - {"result": [ ...videos... ]}  (type-scoped search)
    Walk both, keeping only dicts that look like videos (carry a bvid/arcurl).
    """
    result = payload.get("result")
    entries: List[Dict[str, Any]] = []
    if isinstance(result, list):
        for group in result:
            if not isinstance(group, dict):
                continue
            if group.get("result_type") == "video" and isinstance(group.get("data"), list):
                entries.extend(d for d in group["data"] if isinstance(d, dict))
            elif group.get("bvid") or group.get("arcurl"):
                entries.append(group)
    # Fallback: a flat list under a different key.
    if not entries:
        for key in ("data", "list", "archives"):
            maybe = payload.get(key)
            if isinstance(maybe, list):
                entries.extend(d for d in maybe if isinstance(d, dict) and (d.get("bvid") or d.get("arcurl")))
                if entries:
                    break
    return entries


def _parse_items(entries: List[Dict[str, Any]], core_topic: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw in entries:
        bvid = str(raw.get("bvid") or "").strip()
        if not bvid:
            continue
        title = _clean_html(str(raw.get("title") or ""))
        description = _clean_html(str(raw.get("description") or raw.get("desc") or ""))
        author = str(raw.get("author") or raw.get("uname") or "").strip()

        url = str(raw.get("arcurl") or raw.get("url") or "").strip()
        if not url:
            url = f"https://www.bilibili.com/video/{bvid}"

        date_str = dates.timestamp_to_date(raw.get("pubdate") or raw.get("ctime"))

        views = _to_int(raw.get("play") or raw.get("view"))
        danmaku = _to_int(raw.get("video_review") or raw.get("danmaku"))
        favorites = _to_int(raw.get("favorites") or raw.get("favorite"))
        likes = _to_int(raw.get("like"))
        comments = _to_int(raw.get("review") or raw.get("reply"))

        tags = [t for t in str(raw.get("tag") or "").split(",") if t.strip()]
        relevance = _compute_relevance(core_topic, f"{title} {description}", tags)

        items.append({
            "video_id": bvid,
            "title": title or f"Bilibili video {bvid}",
            "description": description,
            "url": url,
            "channel_name": author,
            "date": date_str,
            "engagement": {
                "views": views,
                "likes": likes,
                "comments": comments,
                "danmaku": danmaku,
                "favorites": favorites,
            },
            "relevance": relevance,
            "why_relevant": f"Bilibili: {title[:60]}" if title else f"Bilibili: {core_topic}",
            "transcript_snippet": "",  # populated by fetch_subtitles
            "cid": raw.get("cid") or raw.get("id"),
        })
    return items


def search_bilibili(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Search Bilibili videos via TikHub general search."""
    if not token:
        return {"items": [], "error": "No TIKHUB_API_KEY configured"}

    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    _log(f"Searching Bilibili for '{topic}' (depth={depth})")

    entries: List[Dict[str, Any]] = []
    last_error = None
    for page in range(1, config["pages"] + 1):
        try:
            resp = http.get(
                f"{TIKHUB_BASE}/fetch_general_search",
                params={
                    "keyword": topic,
                    "order": "totalrank",
                    "page": page,
                    "page_size": config["results"],
                    "pubtime_begin_s": _date_to_unix(from_date),
                    "pubtime_end_s": _date_to_unix(to_date),
                },
                headers=http.tikhub_headers(token),
                timeout=30,
                retries=2,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            _log(f"TikHub search error (page {page}): {e}")
            break
        page_entries = _extract_video_entries(_unwrap(resp))
        if not page_entries:
            break
        entries.extend(page_entries)

    items = _parse_items(entries[:config["results"] * config["pages"]], topic)

    # Hard date filter, with graceful keep-all when nothing falls in range.
    in_range = [i for i in items if i["date"] and from_date <= i["date"] <= to_date]
    if in_range:
        items = in_range
    elif items:
        _log(f"No videos within date range, keeping all {len(items)}")

    items.sort(key=lambda x: x["engagement"]["views"], reverse=True)
    _log(f"Found {len(items)} Bilibili videos")
    return {"items": items, "error": last_error if not items else None}


def fetch_subtitles(
    items: List[Dict[str, Any]],
    token: str,
    depth: str = "default",
) -> Dict[str, str]:
    """Fetch CC/AI subtitles for the top N videos via TikHub.

    Returns a map of bvid -> subtitle text (truncated). Best-effort: many
    videos have no subtitle track, in which case the description carries the
    content signal.
    """
    if not items or not token:
        return {}
    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    top = items[:config["max_subtitles"]]
    _log(f"Fetching subtitles for {len(top)} videos")

    subtitles: Dict[str, str] = {}
    for item in top:
        bvid = item.get("video_id")
        if not bvid:
            continue
        try:
            resp = http.get(
                f"{TIKHUB_BASE}/fetch_video_subtitle",
                params={"bv_id": bvid},
                headers=http.tikhub_headers(token),
                timeout=20,
                retries=1,
            )
        except Exception as e:
            _log(f"Subtitle fetch failed for {bvid}: {e}")
            continue
        text = _parse_subtitle(_unwrap(resp))
        if text:
            words = text.split()
            if len(words) > SUBTITLE_MAX_WORDS:
                text = " ".join(words[:SUBTITLE_MAX_WORDS]) + "..."
            subtitles[bvid] = text

    got = sum(1 for v in subtitles.values() if v)
    _log(f"Got subtitles for {got}/{len(top)} videos")
    return subtitles


def _parse_subtitle(payload: Any) -> str:
    """Extract plaintext from a Bilibili subtitle payload.

    Subtitle JSON typically carries a ``body`` list of {from, to, content}.
    """
    if not isinstance(payload, dict):
        return ""
    body = payload.get("body")
    if not isinstance(body, list):
        for key in ("subtitle", "subtitles", "data"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("body"), list):
                body = inner["body"]
                break
    if not isinstance(body, list):
        return ""
    parts = [str(line.get("content") or "").strip() for line in body if isinstance(line, dict)]
    return " ".join(p for p in parts if p)


def _total_engagement(item: Dict[str, Any]) -> int:
    eng = item.get("engagement", {})
    return (eng.get("views", 0) or 0) + (eng.get("likes", 0) or 0) + (eng.get("comments", 0) or 0)


def enrich_with_comments(
    items: List[Dict[str, Any]],
    token: str,
    max_posts: int = 3,
    max_comments: int = 5,
) -> List[Dict[str, Any]]:
    """Attach top comments to the highest-engagement videos (mirrors youtube_yt)."""
    if not items or not token or max_posts <= 0:
        return items
    ranked = sorted(items, key=_total_engagement, reverse=True)[:max_posts]
    _log(f"Enriching comments for {len(ranked)} Bilibili videos")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _enrich_one(item: dict) -> bool:
        bvid = item.get("video_id")
        if not bvid:
            return False
        try:
            comments = _fetch_comments(bvid, token, max_comments)
            if comments:
                item["top_comments"] = comments
                return True
        except Exception as exc:
            _log(f"Comment enrichment failed for {bvid}: {exc}")
        return False

    enriched = 0
    with ThreadPoolExecutor(max_workers=min(4, len(ranked))) as executor:
        futures = {executor.submit(_enrich_one, item): item for item in ranked}
        for future in as_completed(futures):
            if future.result():
                enriched += 1
    _log(f"Enriched {enriched}/{len(ranked)} videos with comments")
    return items


def _fetch_comments(bvid: str, token: str, max_comments: int) -> List[Dict[str, Any]]:
    """Fetch top comments for one video. Returns [] on any error."""
    try:
        resp = http.get(
            f"{TIKHUB_BASE}/fetch_video_comments",
            params={"bv_id": bvid, "pn": 1},
            headers=http.tikhub_headers(token),
            timeout=30,
            retries=2,
        )
    except Exception as exc:
        _log(f"Comment fetch error for {bvid}: {exc}")
        return []

    payload = _unwrap(resp)
    replies = payload.get("replies")
    if not isinstance(replies, list):
        inner = payload.get("data")
        replies = inner.get("replies") if isinstance(inner, dict) else None
    if not isinstance(replies, list):
        return []

    replies = sorted(replies, key=lambda c: _to_int(c.get("like")), reverse=True)
    out: List[Dict[str, Any]] = []
    for c in replies[:max_comments]:
        if not isinstance(c, dict):
            continue
        content = c.get("content") if isinstance(c.get("content"), dict) else {}
        text = str(content.get("message") or "").strip()
        if not text:
            continue
        member = c.get("member") if isinstance(c.get("member"), dict) else {}
        date_str = dates.timestamp_to_date(c.get("ctime")) or ""
        out.append({
            "author": str(member.get("uname") or ""),
            "text": text[:400],
            "likes": _to_int(c.get("like")),
            "date": date_str,
        })
    return out


def search_and_enrich(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Full Bilibili flow: search, fetch subtitles for top videos, attach them."""
    result = search_bilibili(topic, from_date, to_date, depth, token)
    items = result.get("items", [])
    if not items:
        return {"items": [], "error": result.get("error")}

    subtitles = fetch_subtitles(items, token, depth)
    if subtitles:
        from . import youtube_yt
        for item in items:
            sub = subtitles.get(item["video_id"])
            if sub:
                item["transcript_snippet"] = sub
                try:
                    highlights = youtube_yt.extract_transcript_highlights(sub, topic)
                    if highlights:
                        item["transcript_highlights"] = highlights
                except Exception:
                    pass
    return {"items": items, "error": result.get("error")}


def parse_bilibili_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the normalized item list from a search_and_enrich result."""
    return response.get("items", [])
