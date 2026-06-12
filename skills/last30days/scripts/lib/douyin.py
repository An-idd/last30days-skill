"""Douyin (抖音) search and enrichment via the TikHub API for /last30days.

Douyin is the mainland-China origin of TikTok and shares the same `aweme`
data model (aweme_id, desc, statistics.digg_count/comment_count, author,
text_extra hashtags), so this adapter mirrors tiktok.py field-for-field and
items normalize through the shared short-form-video normalizer.

Backend: TikHub (https://api.tikhub.io), Bearer auth — one TIKHUB_API_KEY also
covers the Bilibili source.
  - search:   POST /api/v1/douyin/search/fetch_video_search_v2  {keyword, ...}
  - comments: GET  /api/v1/douyin/web/fetch_video_comments      ?aweme_id=...

Pay-as-you-go (~$0.001/request). Douyin web rarely exposes spoken-word
transcripts, so content signal comes from the caption (desc) + top comments.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from . import dates, http, log
from .relevance import token_overlap_relevance as _compute_relevance

TIKHUB_BASE = "https://api.tikhub.io"
SEARCH_URL = f"{TIKHUB_BASE}/api/v1/douyin/search/fetch_video_search_v2"
COMMENTS_URL = f"{TIKHUB_BASE}/api/v1/douyin/web/fetch_video_comments"

DEPTH_CONFIG = {
    "quick":   {"results": 10, "pages": 1},
    "default": {"results": 20, "pages": 1},
    "deep":    {"results": 30, "pages": 2},
}

CAPTION_MAX_WORDS = 500


def _log(msg: str):
    log.source_log("Douyin", msg)


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


def _unwrap_awemes(resp: Any) -> List[Dict[str, Any]]:
    """Locate the list of aweme dicts in a TikHub Douyin search response.

    TikHub wraps the raw Douyin payload as {code, data: {...}}. The aweme list
    has appeared under data.data, data.aweme_list, and data.business_data in
    the wild; each entry is either the aweme itself or {aweme_info: aweme}.
    """
    if not isinstance(resp, dict):
        return []
    data = resp.get("data", resp)
    candidates: List[Any] = []
    if isinstance(data, dict):
        for key in ("data", "aweme_list", "business_data", "list"):
            maybe = data.get(key)
            if isinstance(maybe, list) and maybe:
                candidates = maybe
                break
    elif isinstance(data, list):
        candidates = data

    awemes: List[Dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        info = entry.get("aweme_info") if isinstance(entry.get("aweme_info"), dict) else entry
        if isinstance(info, dict) and (info.get("aweme_id") or info.get("desc")):
            awemes.append(info)
    return awemes


def _parse_items(awemes: List[Dict[str, Any]], core_topic: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw in awemes:
        aweme_id = str(raw.get("aweme_id") or "").strip()
        if not aweme_id:
            continue
        text = str(raw.get("desc") or "").strip()

        stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
        play = _to_int(stats.get("play_count"))
        likes = _to_int(stats.get("digg_count"))
        comments = _to_int(stats.get("comment_count"))
        shares = _to_int(stats.get("share_count"))

        author_raw = raw.get("author")
        if isinstance(author_raw, dict):
            author_name = author_raw.get("unique_id") or author_raw.get("nickname") or ""
        elif isinstance(author_raw, str):
            author_name = author_raw
        else:
            author_name = ""

        text_extra = raw.get("text_extra") or []
        hashtags = [t.get("hashtag_name", "") for t in text_extra
                    if isinstance(t, dict) and t.get("hashtag_name")]

        date_str = dates.timestamp_to_date(raw.get("create_time"))

        share_url = str(raw.get("share_url") or "").split("?")[0]
        url = share_url or (f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "")

        relevance = _compute_relevance(core_topic, text, hashtags)

        items.append({
            "video_id": aweme_id,
            "text": text,
            "url": url,
            "author_name": author_name,
            "date": date_str,
            "engagement": {
                "views": play,
                "likes": likes,
                "comments": comments,
                "shares": shares,
            },
            "hashtags": hashtags,
            "relevance": relevance,
            "why_relevant": f"Douyin: {text[:60]}" if text else f"Douyin: {core_topic}",
            "caption_snippet": text[:CAPTION_MAX_WORDS],
        })
    return items


def search_douyin(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Search Douyin videos via TikHub video search v2."""
    if not token:
        return {"items": [], "error": "No TIKHUB_API_KEY configured"}

    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    _log(f"Searching Douyin for '{topic}' (depth={depth})")

    awemes: List[Dict[str, Any]] = []
    cursor = 0
    last_error = None
    for _ in range(config["pages"]):
        try:
            resp = http.post(
                SEARCH_URL,
                json_data={
                    "keyword": topic,
                    "cursor": cursor,
                    "sort_type": "0",       # 0 = comprehensive (recency-weighted)
                    "publish_time": "0",    # 0 = no server filter; we date-filter below
                },
                headers=http.tikhub_headers(token),
                timeout=30,
                retries=2,
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            _log(f"TikHub search error: {e}")
            break
        page = _unwrap_awemes(resp)
        if not page:
            break
        awemes.extend(page)
        cursor += len(page)
        if len(awemes) >= config["results"] * config["pages"]:
            break

    items = _parse_items(awemes[:config["results"] * config["pages"]], topic)

    # Hard date filter, keep-all fallback when nothing lands in the window.
    in_range = [i for i in items if i["date"] and from_date <= i["date"] <= to_date]
    if in_range:
        items = in_range
    elif items:
        _log(f"No videos within date range, keeping all {len(items)}")

    items.sort(key=lambda x: x["engagement"]["likes"], reverse=True)
    _log(f"Found {len(items)} Douyin videos")
    return {"items": items, "error": last_error if not items else None}


def _total_engagement(item: Dict[str, Any]) -> int:
    eng = item.get("engagement", {})
    return (eng.get("likes", 0) or 0) + (eng.get("comments", 0) or 0) + (eng.get("views", 0) or 0)


def enrich_with_comments(
    items: List[Dict[str, Any]],
    token: str,
    max_posts: int = 3,
    max_comments: int = 5,
) -> List[Dict[str, Any]]:
    """Attach top comments to the highest-engagement videos (mirrors tiktok)."""
    if not items or not token or max_posts <= 0:
        return items
    ranked = sorted(items, key=_total_engagement, reverse=True)[:max_posts]
    _log(f"Enriching comments for {len(ranked)} Douyin videos")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _enrich_one(item: dict) -> bool:
        aweme_id = item.get("video_id")
        if not aweme_id:
            return False
        try:
            comments = _fetch_comments(aweme_id, token, max_comments)
            if comments:
                item["top_comments"] = comments
                return True
        except Exception as exc:
            _log(f"Comment enrichment failed for {aweme_id}: {exc}")
        return False

    enriched = 0
    with ThreadPoolExecutor(max_workers=min(4, len(ranked))) as executor:
        futures = {executor.submit(_enrich_one, item): item for item in ranked}
        for future in as_completed(futures):
            if future.result():
                enriched += 1
    _log(f"Enriched {enriched}/{len(ranked)} videos with comments")
    return items


def _fetch_comments(aweme_id: str, token: str, max_comments: int) -> List[Dict[str, Any]]:
    """Fetch top comments for one video. Returns [] on any error."""
    try:
        data = http.get(
            COMMENTS_URL,
            params={"aweme_id": aweme_id, "cursor": 0, "count": max(20, max_comments)},
            headers=http.tikhub_headers(token),
            timeout=30,
            retries=2,
        )
    except Exception as exc:
        _log(f"Comment fetch error for {aweme_id}: {exc}")
        return []

    payload = data.get("data") if isinstance(data, dict) else None
    raw_comments = []
    if isinstance(payload, dict):
        raw_comments = payload.get("comments") or payload.get("data") or []
    elif isinstance(data, dict):
        raw_comments = data.get("comments") or []
    if not isinstance(raw_comments, list):
        return []

    raw_comments = sorted(
        raw_comments,
        key=lambda c: _to_int(c.get("digg_count")) if isinstance(c, dict) else 0,
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for c in raw_comments[:max_comments]:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        author = user.get("nickname") or user.get("unique_id") or ""
        date_str = dates.timestamp_to_date(c.get("create_time")) or ""
        out.append({
            "author": str(author),
            "text": text[:400],
            "digg_count": _to_int(c.get("digg_count")),
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
    """Full Douyin flow: search, then attach top comments to top videos."""
    result = search_douyin(topic, from_date, to_date, depth, token)
    items = result.get("items", [])
    if not items:
        return {"items": [], "error": result.get("error")}
    enrich_with_comments(items, token)
    return {"items": items, "error": result.get("error")}


def parse_douyin_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the normalized item list from a search_and_enrich result."""
    return response.get("items", [])
