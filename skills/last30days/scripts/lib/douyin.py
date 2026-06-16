"""Douyin (抖音, Chinese TikTok) search via the Apify MCP actor for /last30days.

Uses the ``zen-studio/douyin-search-scraper`` Apify actor to search Douyin by
keyword, extract engagement metrics (views, likes, comments, shares), and pull
video captions. Chinese, English, hashtags, and brand names all work as queries.

Requires APIFY_API_TOKEN in config. Apify actor (run via the standard REST
``run-sync-get-dataset-items`` endpoint, which is also what the MCP server at
https://mcp.apify.com/?tools=actors,docs,zen-studio/douyin-search-scraper
invokes under the hood):
    https://apify.com/zen-studio/douyin-search-scraper
"""

from typing import Any, Dict, List, Optional, Set

from . import dates, http, log
from .relevance import token_overlap_relevance as _compute_relevance

# Actor ids with the slash encoded as `~` for the REST path.
DOUYIN_ACTOR = "zen-studio~douyin-search-scraper"
COMMENTS_ACTOR = "zen-studio~douyin-comments-scraper"
APIFY_BASE = "https://api.apify.com/v2"

# Comment-enrichment defaults: how many top videos to fetch comments for and
# how many comments to keep per video. Billed at ~$5.99 / 1,000 comments, so
# kept conservative (3 x 10 = ~30 comments ≈ $0.18 per run).
COMMENT_MAX_POSTS = 3
COMMENT_MAX_PER_POST = 10

# Depth configurations: how many results to fetch / captions to keep.
DEPTH_CONFIG = {
    "quick":   {"max_results": 15, "max_captions": 3},
    "default": {"max_results": 30, "max_captions": 5},
    "deep":    {"max_results": 60, "max_captions": 8},
}

# Max words to keep from each caption.
CAPTION_MAX_WORDS = 500


def _log(msg: str):
    log.source_log("Douyin", msg)


def _extract_core_subject(topic: str) -> str:
    """Extract core subject from a verbose query for Douyin search."""
    from .query import extract_core_subject
    _DOUYIN_NOISE = frozenset({
        'best', 'top', 'latest', 'new', 'news', 'update', 'updates',
        'trending', 'hottest', 'popular', 'viral',
        'recommendations', 'advice', 'review', 'reviews',
        'methods', 'strategies', 'approaches',
    })
    return extract_core_subject(topic, noise=_DOUYIN_NOISE)


def expand_douyin_queries(topic: str, depth: str) -> List[str]:
    """Generate Douyin keyword queries from a topic.

    Douyin's audience is largely Chinese, but the actor accepts Chinese,
    English, hashtags, and brand names. We keep this lightweight: the core
    subject plus the cleaned original topic when distinct. The actor itself
    handles relevance ranking, so over-expanding only wastes credits.

    Returns 1-2 query strings depending on depth.
    """
    core = _extract_core_subject(topic)
    queries = [core]

    original_clean = topic.strip().rstrip('?!.')
    if core.lower() != original_clean.lower() and len(original_clean.split()) <= 8:
        queries.append(original_clean)

    caps = {"quick": 1, "default": 2, "deep": 2}
    cap = caps.get(depth, 2)
    return queries[:cap]


def _publish_time(from_date: str, to_date: str) -> str:
    """Map the requested date window onto the actor's publishTime buckets.

    The actor only supports unlimited / one_day / one_week / half_year, so we
    pick the smallest bucket that still covers the window and rely on a hard
    date filter afterward to trim anything outside [from_date, to_date].
    """
    start = dates.parse_date(from_date)
    end = dates.parse_date(to_date)
    if not start or not end:
        return "half_year"
    span = (end - start).days
    if span <= 1:
        return "one_day"
    if span <= 7:
        return "one_week"
    if span <= 183:
        return "half_year"
    return "unlimited"


def _parse_date(item: Dict[str, Any]) -> Optional[str]:
    """Parse a Douyin item's publish date to YYYY-MM-DD."""
    ts = item.get("createTime")
    if ts:
        try:
            return dates.timestamp_to_date(int(ts))
        except (ValueError, TypeError):
            pass
    create_date = item.get("createDate")
    if isinstance(create_date, str) and len(create_date) >= 10:
        return create_date[:10]
    return None


def _author_handle(item: Dict[str, Any]) -> str:
    """Extract the creator label from authorMeta.

    Unlike TikTok (where ``unique_id`` is the vanity @handle), Douyin's
    ``username`` is a numeric 抖音号; the human-readable nickname lives in
    ``name``. Prefer the nickname for display, falling back to the id.
    """
    author = item.get("authorMeta")
    if isinstance(author, dict):
        return str(author.get("name") or author.get("username") or "")
    if isinstance(author, str):
        return author
    return ""


def _engagement_rank(item: Dict[str, Any]) -> int:
    """Ranking signal for Douyin items.

    Douyin search results almost always report ``playCount: 0`` (public view
    counts are hidden), so sorting by views is useless. Rank by likes +
    comments + collects + shares instead.
    """
    eng = item.get("engagement", {})
    return (
        (eng.get("likes") or 0)
        + (eng.get("comments") or 0)
        + (eng.get("collects") or 0)
        + (eng.get("shares") or 0)
    )


def _hashtag_names(item: Dict[str, Any]) -> List[str]:
    """Extract hashtag names whether the actor returns strings or dicts."""
    out: List[str] = []
    for tag in item.get("hashtags") or []:
        if isinstance(tag, str) and tag:
            out.append(tag.lstrip("#"))
        elif isinstance(tag, dict):
            name = tag.get("name") or tag.get("hashtagName") or tag.get("title")
            if name:
                out.append(str(name).lstrip("#"))
    return out


def _parse_items(raw_items: List[Dict[str, Any]], core_topic: str) -> List[Dict[str, Any]]:
    """Parse raw Apify Douyin dataset items into normalized dicts."""
    items: List[Dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        video_id = str(raw.get("id") or "")
        text = str(raw.get("text") or raw.get("caption") or "")

        stats = raw.get("statistics") if isinstance(raw.get("statistics"), dict) else {}
        play_count = stats.get("playCount") or stats.get("play_count") or 0
        digg_count = stats.get("diggCount") or stats.get("digg_count") or 0
        comment_count = stats.get("commentCount") or stats.get("comment_count") or 0
        share_count = stats.get("shareCount") or stats.get("share_count") or 0
        collect_count = stats.get("collectCount") or stats.get("collect_count") or 0

        hashtag_names = _hashtag_names(raw)
        author_name = _author_handle(raw)
        url = str(raw.get("url") or raw.get("shareUrl") or "").split("?")[0]
        date_str = _parse_date(raw)

        relevance = _compute_relevance(core_topic, text, hashtag_names)

        items.append({
            "id": video_id,
            "text": text,
            "url": url,
            "author_name": author_name,
            "date": date_str,
            "engagement": {
                "views": play_count,
                "likes": digg_count,
                "comments": comment_count,
                "shares": share_count,
                "collects": collect_count,
            },
            "hashtags": hashtag_names,
            "relevance": relevance,
            "why_relevant": f"Douyin: {text[:60]}" if text else f"Douyin: {core_topic}",
            "caption_snippet": "",  # populated below from caption/text
        })
    return items


def search_douyin(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Search Douyin via the Apify ``zen-studio/douyin-search-scraper`` actor.

    Args:
        topic: Search topic (keyword).
        from_date: Start date (YYYY-MM-DD).
        to_date: End date (YYYY-MM-DD).
        depth: 'quick', 'default', or 'deep'.
        token: Apify API token.

    Returns:
        Dict with 'items' list and optional 'error'.
    """
    if not token:
        return {"items": [], "error": "No APIFY_API_TOKEN configured"}

    config = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    core_topic = _extract_core_subject(topic)

    _log(f"Searching Douyin for '{core_topic}' (depth={depth}, count={config['max_results']})")

    payload = {
        "keywords": [core_topic],
        "maxResultsPerQuery": config["max_results"],
        "sort": "general",
        "publishTime": _publish_time(from_date, to_date),
    }

    try:
        # run-sync-get-dataset-items blocks until the run finishes and returns
        # the dataset items array directly (the actor scrape can take a while,
        # so the timeout is generous and retries are kept low).
        data = http.post(
            f"{APIFY_BASE}/acts/{DOUYIN_ACTOR}/run-sync-get-dataset-items",
            json_data=payload,
            params={"token": token},
            timeout=180,
            retries=2,
        )
    except Exception as e:
        _log(f"Apify error: {e}")
        return {"items": [], "error": f"{type(e).__name__}: {e}"}

    # run-sync-get-dataset-items returns a bare JSON array of dataset items.
    if isinstance(data, list):
        raw_items = data
    else:
        raw_items = data.get("items") or data.get("data") or []
    raw_items = raw_items[:config["max_results"]]

    items = _parse_items(raw_items, core_topic)

    # Hard date filter (the publishTime bucket is coarser than the real window).
    in_range = [i for i in items if i["date"] and from_date <= i["date"] <= to_date]
    out_of_range = len(items) - len(in_range)
    if in_range:
        items = in_range
        if out_of_range:
            _log(f"Filtered {out_of_range} videos outside date range")
    else:
        _log(f"No videos within date range, keeping all {len(items)}")

    items.sort(key=_engagement_rank, reverse=True)

    _log(f"Found {len(items)} Douyin videos")
    return {"items": items}


def search_and_enrich(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = None,
) -> Dict[str, Any]:
    """Full Douyin search: run expanded keyword queries and merge results.

    Mirrors tiktok.search_and_enrich() but the Apify actor already returns the
    video caption inline, so there is no separate transcript-fetch pass.

    Args:
        topic: Search topic (raw topic, not the planner's narrowed query).
        from_date: Start date (YYYY-MM-DD).
        to_date: End date (YYYY-MM-DD).
        depth: 'quick', 'default', or 'deep'.
        token: Apify API token.

    Returns:
        Dict with 'items' list. Each item has a 'caption_snippet' field.
    """
    seen_ids: Set[str] = set()
    items: List[Dict[str, Any]] = []
    last_error = None

    for q in expand_douyin_queries(topic, depth):
        result = search_douyin(q, from_date, to_date, depth, token)
        if result.get("error"):
            last_error = result["error"]
        for item in result.get("items", []):
            vid = item.get("id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                items.append(item)

    items.sort(key=_engagement_rank, reverse=True)

    if not items:
        return {"items": [], "error": last_error}

    # The caption is already inline in the search payload — use the description
    # text as the caption snippet (truncated), matching TikTok's snippet shape.
    for item in items:
        text = item.get("text", "")
        if text:
            words = text.split()
            if len(words) > CAPTION_MAX_WORDS:
                text = ' '.join(words[:CAPTION_MAX_WORDS]) + '...'
            item["caption_snippet"] = text

    return {"items": items, "error": last_error}


def parse_douyin_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a Douyin search response into the list ready for normalization."""
    return response.get("items", [])


def _fetch_comments(
    aweme_urls: List[str],
    token: str,
    max_comments: int,
) -> List[Dict[str, Any]]:
    """Fetch comments for multiple Douyin videos in one Apify run.

    The zen-studio/douyin-comments-scraper actor accepts an array of video
    URLs (``awemeUrls``), so all top videos are fetched in a single run rather
    than one run per video. Each output row carries ``awemeId`` so callers can
    group comments back to their parent video.

    Returns the raw comment rows, or an empty list on any error — comment
    failures must never crash the pipeline.
    """
    payload = {
        "awemeUrls": aweme_urls,
        "maxCommentsPerAweme": max_comments,
        "includeReplies": False,  # top-level high-likes comments only
    }
    try:
        data = http.post(
            f"{APIFY_BASE}/acts/{COMMENTS_ACTOR}/run-sync-get-dataset-items",
            json_data=payload,
            params={"token": token},
            timeout=180,
            retries=1,
        )
    except Exception as exc:
        _log(f"Comment fetch error: {exc}")
        return []
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("data") or []


def _comment_engagement(item: Dict[str, Any]) -> int:
    """Total engagement for ranking which videos deserve comment enrichment."""
    eng = item.get("engagement", {})
    return (
        (eng.get("likes") or 0)
        + (eng.get("comments") or 0)
        + (eng.get("collects") or 0)
    )


def enrich_with_comments(
    items: List[Dict[str, Any]],
    token: str,
    max_posts: int = COMMENT_MAX_POSTS,
    max_comments: int = COMMENT_MAX_PER_POST,
) -> List[Dict[str, Any]]:
    """Attach top high-likes comments to the most-engaged Douyin videos.

    Mirrors tiktok.enrich_with_comments: ranks videos by engagement, fetches
    comments for the top N in a single Apify run, and attaches a ``top_comments``
    list (shape: author / text / digg_count / date) to each enriched item — the
    same shape TikTok produces, so normalize._remap_comments handles it as-is.

    Args:
        items: Douyin items from search_and_enrich().
        token: Apify API token.
        max_posts: How many top videos to enrich with comments.
        max_comments: Max comments to keep per video.

    Returns:
        Items list (mutated in place) with top_comments added to enriched items.
    """
    if not items or not token or max_posts <= 0:
        return items

    ranked = sorted(items, key=_comment_engagement, reverse=True)
    top_items = [i for i in ranked[:max_posts] if i.get("url")]
    if not top_items:
        return items

    _log(f"Enriching comments for {len(top_items)} Douyin videos")
    rows = _fetch_comments([i["url"] for i in top_items], token, max_comments)
    if not rows:
        return items

    # Group comment rows back to their parent video by awemeId.
    by_aweme: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        by_aweme.setdefault(str(row.get("awemeId") or ""), []).append(row)

    enriched = 0
    for item in top_items:
        comments = by_aweme.get(str(item.get("id") or ""), [])
        comments.sort(key=lambda c: c.get("likeCount", 0) or 0, reverse=True)
        mapped: List[Dict[str, Any]] = []
        for c in comments[:max_comments]:
            text = c.get("text") or ""
            if not text:
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            date_str = c.get("createDate") or ""
            if not date_str and c.get("createTime"):
                try:
                    date_str = dates.timestamp_to_date(int(c["createTime"])) or ""
                except (ValueError, TypeError):
                    date_str = ""
            mapped.append({
                "author": str(user.get("nickname") or ""),
                "text": text[:400],
                "digg_count": c.get("likeCount", 0) or 0,
                "date": date_str,
            })
        if mapped:
            item["top_comments"] = mapped
            enriched += 1

    _log(f"Enriched {enriched}/{len(top_items)} videos with comments")
    return items
