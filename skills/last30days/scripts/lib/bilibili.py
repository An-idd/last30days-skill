"""Bilibili search adapter for last30days.

Uses Bilibili's public web search API:
  GET https://api.bilibili.com/x/web-interface/wbi/search/type

WBI-signed via bilibili_wbi.sign_params. buvid3 cookie is auto-bootstrapped
on first use (no user login required). Module-level caches keep the
bootstrap to one-shot per Python process.

Modeled after skills/last30days/scripts/lib/xiaohongshu_api.py.
"""

import json
import math
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import bilibili_wbi, http

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
BOOTSTRAP_URL = "https://www.bilibili.com/"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_EM_TAG_PATTERN = re.compile(r"</?em[^>]*>")


def _to_int(value: Any) -> int:
    """Convert engagement count to int. Supports plain ints/strings and
    Chinese suffixes 万 (x10^4) and 亿 (x10^8).

    Private to this module for the first PR. A future PR may extract this
    to lib/normalize.py once Weibo/Zhihu adapters share the pattern.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().replace(",", "")
    if not text:
        return 0

    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10_000)
        if text.endswith("亿"):
            return int(float(text[:-1]) * 100_000_000)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _relevance_from_interactions(play: int, like: int, danmaku: int, favorite: int) -> float:
    """Heuristic 0.05-1.0 relevance from B站 engagement signals.

    Weights (descending): favorite > like > danmaku > play.
    Rationale: favorites imply retention intent -- the strongest signal the
    search API exposes. Likes are light affirmation. Danmaku is B站-specific
    participation. Plays are passive baseline.

    Note: coins are B站's strongest endorsement, but /search/type does not
    return a coin count, so they are deliberately absent from this formula.
    Coin-weighted scoring belongs with the per-video enrichment follow-up
    that fetches full stats.

    Uses a log10 compression on the weighted sum so that engagement scales
    smoothly from a tiny video (~0.05) to a viral hit (~1.0), with a
    moderately popular video landing near 0.5.
    """
    weighted = play * 1.0 + like * 2.0 + danmaku * 1.5 + favorite * 2.5
    if weighted <= 0:
        return 0.05
    # log10(weighted) maps: 10 -> 1, 1k -> 3, 1M -> 6, 10M -> 7. Divide by
    # 7.5 so a moderately popular video (~100k weighted) lands near 0.7
    # and a viral one (10M+) saturates at 1.0.
    score = math.log10(1.0 + weighted) / 7.5
    return round(min(1.0, max(0.05, score)), 3)


def _parse_duration(value: Any) -> int:
    """Parse '4:05' or '1:02:03' style duration to seconds. 0 on failure."""
    if not isinstance(value, str) or not value:
        return 0
    parts = value.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _pubdate_to_iso(value: Any) -> Optional[str]:
    """Convert a Unix epoch (int seconds) to YYYY-MM-DD UTC. None on failure."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    if iv <= 0:
        return None
    try:
        return datetime.fromtimestamp(iv, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return None


def _strip_em_tags(text: Any) -> str:
    """Remove <em>...</em> search-highlight tags from a string."""
    if not isinstance(text, str):
        return ""
    return _EM_TAG_PATTERN.sub("", text)


# Module-level cache: bootstrap only once per Python process.
_mixin_key_cache: Optional[str] = None
_cookie_jar_cache: Optional[Dict[str, str]] = None


def _cookie_header(cookies: Dict[str, str]) -> str:
    """Render a cookie jar dict as a 'k1=v1; k2=v2' Cookie header value."""
    return "; ".join("{}={}".format(k, v) for k, v in cookies.items())


def _reset_session_cache() -> None:
    """Clear cached bootstrap state. Test-only helper."""
    global _mixin_key_cache, _cookie_jar_cache
    _mixin_key_cache = None
    _cookie_jar_cache = None


def _fetch_buvid3() -> str:
    """GET https://www.bilibili.com/ and extract buvid3 from Set-Cookie.

    Uses urllib directly because http.py does not expose response headers.
    Returns the buvid3 cookie value; raises http.HTTPError on failure
    (with a "cookie bootstrap failed" prefix so callers can distinguish
    it from search-time errors).
    """
    req = urllib.request.Request(
        BOOTSTRAP_URL,
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            set_cookies = resp.headers.get_all("Set-Cookie") or []
    except (urllib.error.URLError, OSError) as exc:
        raise http.HTTPError(
            "Bilibili: cookie bootstrap failed: {}".format(exc)
        ) from exc

    for raw in set_cookies:
        first = raw.split(";", 1)[0].strip()
        if "=" in first:
            name, value = first.split("=", 1)
            if name.strip() == "buvid3" and value.strip():
                return value.strip()
    raise http.HTTPError(
        "Bilibili: cookie bootstrap failed: buvid3 not in Set-Cookie"
    )


def _ensure_session() -> Tuple[str, Dict[str, str]]:
    """Return (mixin_key, cookie_jar). Bootstrap once, then cache."""
    global _mixin_key_cache, _cookie_jar_cache
    if _mixin_key_cache and _cookie_jar_cache:
        return _mixin_key_cache, _cookie_jar_cache

    try:
        buvid3 = _fetch_buvid3()  # raises pre-tagged HTTPError on failure
        cookies = {"buvid3": buvid3}
        try:
            nav_json = http.get(
                NAV_URL,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                    "Cookie": _cookie_header(cookies),
                },
                timeout=10,
                retries=2,
            )
        except http.HTTPError as exc:
            raise http.HTTPError(
                "Bilibili: cookie bootstrap failed: nav fetch error: {}".format(exc)
            ) from exc

        if not isinstance(nav_json, dict):
            raise http.HTTPError(
                "Bilibili: cookie bootstrap failed: "
                "nav response not a dict: {}".format(type(nav_json).__name__)
            )
        # The /x/web-interface/nav endpoint returns code=-101 "not logged in"
        # for anonymous callers, but the wbi_img keys we need are still present
        # in the response data. Let get_wbi_keys() validate the actual shape
        # we depend on, rather than gating on the top-level status code.
        img_key, sub_key = bilibili_wbi.get_wbi_keys(nav_json)
        mixin = bilibili_wbi.mix_key(img_key, sub_key)
    except http.HTTPError:
        raise  # already tagged at the source
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        # IndexError guards malformed WBI keys: if nav returns img_key+sub_key
        # shorter than the 64 positions MIXIN_KEY_ENC_TAB indexes, mix_key()
        # raises IndexError, which we surface as a clean bootstrap failure.
        raise http.HTTPError(
            "Bilibili: cookie bootstrap failed: {}".format(exc)
        ) from exc

    _mixin_key_cache = mixin
    _cookie_jar_cache = cookies
    return mixin, cookies


_DEPTH_PAGE_SIZE = {"quick": 10, "default": 20, "deep": 30}


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_video_url(bvid: str) -> str:
    return "https://www.bilibili.com/video/{}".format(bvid)


def search_bilibili(topic: str, from_date: str, to_date: str, depth: str = "default") -> List[Dict[str, Any]]:
    """Search Bilibili videos for `topic`, return normalized web-items.

    Args:
        topic: Search keyword (Chinese or English).
        from_date: Inclusive lower bound, "YYYY-MM-DD".
        to_date: Inclusive upper bound, "YYYY-MM-DD".
        depth: "quick" | "default" | "deep" -> page_size 10/20/30.

    Returns:
        List of normalized item dicts. Empty list if no results match
        the date window.

    Raises:
        http.HTTPError if bootstrap, signing, or search fails. The pipeline
        catches and drops the source for the current run.
    """
    mixin_key, cookies = _ensure_session()
    cookie_header = _cookie_header(cookies)

    params = {
        "search_type": "video",
        "keyword": topic,
        "order": "totalrank",
        "page": 1,
        "page_size": _DEPTH_PAGE_SIZE.get(depth, 20),
    }
    signed = bilibili_wbi.sign_params(params, mixin_key)

    resp = http.get(
        SEARCH_URL,
        params=signed,
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Cookie": cookie_header,
        },
        timeout=15,
        retries=1,
    )

    if not isinstance(resp, dict):
        raise http.HTTPError(
            "Bilibili search: unexpected response shape: {}".format(type(resp).__name__)
        )

    code = resp.get("code", -1)
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    if code != 0:
        # Any non-zero search code means the cached session may be stale: a
        # v_voucher signals WBI key rotation, and other error codes can follow
        # an expired buvid3. Clear the process-level cache so the next call
        # re-bootstraps fresh keys instead of replaying the same failure for
        # the rest of the process lifetime.
        _reset_session_cache()
        if "v_voucher" in data:
            raise http.HTTPError(
                "Bilibili search: WBI signature may have rotated "
                "(code={}, v_voucher present). Re-verify MIXIN_KEY_ENC_TAB "
                "and signing algorithm.".format(code)
            )
        raise http.HTTPError(
            "Bilibili search: code={} message={!r}".format(code, resp.get("message"))
        )

    raw_items = data.get("result", []) if isinstance(data.get("result"), list) else []
    from_d = _parse_date(from_date)
    to_d = _parse_date(to_date)

    out = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _normalize_video(raw, index=len(out) + 1)
        if item is None:
            continue
        # Drop items we can't place in the requested window. B站 search returns
        # an authoritative pubdate for virtually every result, so this only
        # sheds the rare malformed item rather than letting undated content
        # silently bypass the date filter.
        if not item["date"]:
            continue
        d = _parse_date(item["date"])
        if d < from_d or d > to_d:
            continue
        out.append(item)
    return out


def _normalize_video(raw: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    """Convert a raw search result item into the web-item schema."""
    bvid = str(raw.get("bvid") or "").strip()
    if not bvid:
        return None

    title = _strip_em_tags(raw.get("title")).strip()
    # B站 highlights the matched keyword with <em> tags in the description too,
    # not just the title, so strip it here as well before it becomes the snippet.
    description = _strip_em_tags(raw.get("description")).strip()
    pubdate = raw.get("pubdate") or raw.get("senddate")
    iso = _pubdate_to_iso(pubdate)

    play = _to_int(raw.get("play"))
    like = _to_int(raw.get("like"))
    danmaku = _to_int(raw.get("video_review"))
    favorite = _to_int(raw.get("favorites"))
    review = _to_int(raw.get("review"))

    relevance = _relevance_from_interactions(play, like, danmaku, favorite)

    why = (
        "Bilibili engagement: play={}, like={}, danmaku={}, "
        "favorite={}, comments={}".format(play, like, danmaku, favorite, review)
    )

    return {
        "id": "BILI{}".format(index),
        "title": title[:200] if title else "Bilibili video {}".format(bvid),
        "url": _build_video_url(bvid),
        "source_domain": "bilibili.com",
        "snippet": description[:500],
        "date": iso,
        "date_confidence": "high" if iso else "low",
        "relevance": relevance,
        "why_relevant": why,
        # /search/type only returns these counters; coin and share are not in
        # the response, so they are intentionally omitted rather than reported
        # as a misleading zero. A per-video enrichment follow-up can add them.
        "engagement": {
            "play": play,
            "like": like,
            "danmaku": danmaku,
            "favorite": favorite,
            "review": review,
        },
        "extra": {
            "author": str(raw.get("author") or "").strip(),
            "author_mid": _to_int(raw.get("mid")),
            "bvid": bvid,
            "duration": _parse_duration(raw.get("duration")),
        },
    }
