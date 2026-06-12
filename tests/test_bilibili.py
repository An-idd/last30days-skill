"""Tests for skills/last30days/scripts/lib/bilibili.py."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import bilibili, http

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bilibili"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestConstants(unittest.TestCase):
    def test_nav_url(self):
        self.assertEqual(
            "https://api.bilibili.com/x/web-interface/nav",
            bilibili.NAV_URL,
        )

    def test_search_url(self):
        self.assertEqual(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            bilibili.SEARCH_URL,
        )


class TestToInt(unittest.TestCase):
    def test_plain_int(self):
        self.assertEqual(124000, bilibili._to_int(124000))

    def test_plain_str(self):
        self.assertEqual(124000, bilibili._to_int("124000"))

    def test_wan_suffix(self):
        self.assertEqual(12000, bilibili._to_int("1.2万"))
        self.assertEqual(120000, bilibili._to_int("12万"))

    def test_yi_suffix(self):
        self.assertEqual(300000000, bilibili._to_int("3亿"))

    def test_comma_separated(self):
        self.assertEqual(1234, bilibili._to_int("1,234"))

    def test_none(self):
        self.assertEqual(0, bilibili._to_int(None))

    def test_garbage(self):
        self.assertEqual(0, bilibili._to_int("not a number"))
        self.assertEqual(0, bilibili._to_int(""))


class TestRelevance(unittest.TestCase):
    def test_clamped_min_to_0_05(self):
        self.assertEqual(0.05, bilibili._relevance_from_interactions(0, 0, 0, 0))

    def test_clamped_max_to_1_0(self):
        self.assertEqual(
            1.0,
            bilibili._relevance_from_interactions(10**9, 10**9, 10**9, 10**9),
        )

    def test_favorite_weighted_higher_than_like(self):
        base = bilibili._relevance_from_interactions(100, 0, 0, 0)
        with_like = bilibili._relevance_from_interactions(100, 100, 0, 0)
        with_fav = bilibili._relevance_from_interactions(100, 0, 0, 100)
        self.assertGreater(with_fav, with_like)
        self.assertGreater(with_like, base)

    def test_normal_range(self):
        score = bilibili._relevance_from_interactions(
            play=100_000, like=3_000, danmaku=500, favorite=1_500
        )
        self.assertGreaterEqual(score, 0.3)
        self.assertLessEqual(score, 0.7)


class TestParseDuration(unittest.TestCase):
    def test_mmss(self):
        self.assertEqual(245, bilibili._parse_duration("4:05"))

    def test_hhmmss(self):
        self.assertEqual(3723, bilibili._parse_duration("1:02:03"))

    def test_garbage(self):
        self.assertEqual(0, bilibili._parse_duration(""))
        self.assertEqual(0, bilibili._parse_duration(None))
        self.assertEqual(0, bilibili._parse_duration("abc"))


class TestPubdateToIso(unittest.TestCase):
    def test_valid_epoch(self):
        self.assertEqual("2025-05-28", bilibili._pubdate_to_iso(1748390400))

    def test_zero_returns_none(self):
        self.assertIsNone(bilibili._pubdate_to_iso(0))

    def test_invalid_returns_none(self):
        self.assertIsNone(bilibili._pubdate_to_iso("garbage"))
        self.assertIsNone(bilibili._pubdate_to_iso(None))


class TestStripEmTags(unittest.TestCase):
    def test_removes_em_wrappers(self):
        s = '<em class="keyword">长沙</em>麻将入门'
        self.assertEqual("长沙麻将入门", bilibili._strip_em_tags(s))

    def test_handles_multiple(self):
        s = '<em class="keyword">A</em>B<em class="keyword">C</em>D'
        self.assertEqual("ABCD", bilibili._strip_em_tags(s))

    def test_passthrough_when_clean(self):
        self.assertEqual("plain title", bilibili._strip_em_tags("plain title"))

    def test_handles_empty(self):
        self.assertEqual("", bilibili._strip_em_tags(""))
        self.assertEqual("", bilibili._strip_em_tags(None))


class TestEnsureSession(unittest.TestCase):
    def setUp(self):
        bilibili._reset_session_cache()

    def tearDown(self):
        bilibili._reset_session_cache()

    def test_calls_bootstrap_once(self):
        """Two calls -> bootstrap fires once."""
        calls = {"buvid": 0, "nav": 0}

        def fake_fetch_buvid3():
            calls["buvid"] += 1
            return "FAKE_BUVID3_VALUE"

        def fake_http_get(url, headers=None, **kwargs):
            calls["nav"] += 1
            return _load("nav_response.json")

        with patch.object(bilibili, "_fetch_buvid3", fake_fetch_buvid3), \
             patch.object(bilibili.http, "get", fake_http_get):
            a_mixin, a_cookies = bilibili._ensure_session()
            b_mixin, b_cookies = bilibili._ensure_session()

        self.assertEqual(a_mixin, b_mixin)
        self.assertEqual(a_cookies, b_cookies)
        self.assertEqual(1, calls["buvid"])
        self.assertEqual(1, calls["nav"])
        self.assertEqual("FAKE_BUVID3_VALUE", a_cookies["buvid3"])
        self.assertEqual(32, len(a_mixin))

    def test_raises_on_bootstrap_failure(self):
        def fake_fetch_buvid3():
            raise http.HTTPError("Bilibili: cookie bootstrap failed: simulated GET failure")

        with patch.object(bilibili, "_fetch_buvid3", fake_fetch_buvid3):
            with self.assertRaises(http.HTTPError) as ctx:
                bilibili._ensure_session()
        self.assertIn("cookie bootstrap failed", str(ctx.exception))

    def test_raises_on_bad_nav_response(self):
        # No wbi_img in response -> get_wbi_keys raises ValueError ->
        # wrapped as HTTPError by _ensure_session.
        with patch.object(bilibili, "_fetch_buvid3", lambda: "BUVID"), \
             patch.object(bilibili.http, "get", lambda url, **kw: {"code": -1, "data": {}}):
            with self.assertRaises(http.HTTPError):
                bilibili._ensure_session()

    def test_short_wbi_keys_wrapped_as_http_error(self):
        """If nav returns keys shorter than the 64 positions MIXIN_KEY_ENC_TAB
        indexes, mix_key() raises IndexError -> wrapped as a clean HTTPError."""
        short_nav = {
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/a.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/b.png",
                }
            },
        }
        with patch.object(bilibili, "_fetch_buvid3", lambda: "BUVID"), \
             patch.object(bilibili.http, "get", lambda url, **kw: short_nav):
            with self.assertRaises(http.HTTPError) as ctx:
                bilibili._ensure_session()
        self.assertIn("cookie bootstrap failed", str(ctx.exception))

    def test_accepts_anonymous_nav_response(self):
        """Production nav returns code=-101 'not logged in' for anonymous callers,
        but wbi_img is still present. We should accept it as a valid bootstrap."""
        nav = _load("nav_response.json")
        anon_nav = dict(nav)
        anon_nav["code"] = -101
        anon_nav["message"] = "账号未登录"
        anon_nav["data"] = dict(anon_nav["data"])
        anon_nav["data"]["isLogin"] = False

        with patch.object(bilibili, "_fetch_buvid3", lambda: "BUVID"), \
             patch.object(bilibili.http, "get", lambda url, **kw: anon_nav):
            mixin, cookies = bilibili._ensure_session()
        self.assertEqual(32, len(mixin))
        self.assertEqual({"buvid3": "BUVID"}, cookies)


class TestSearchBilibili(unittest.TestCase):
    def setUp(self):
        bilibili._reset_session_cache()

    def tearDown(self):
        bilibili._reset_session_cache()

    def _patch_session(self):
        return patch.object(
            bilibili, "_ensure_session", lambda: ("a" * 32, {"buvid3": "X"})
        )

    def test_normalizes_fixture_response(self):
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: _load("search_video_response.json")):
            items = bilibili.search_bilibili(
                topic="长沙麻将",
                from_date="2025-01-01",
                to_date="2025-12-31",
                depth="default",
            )
        self.assertEqual(2, len(items))
        first = items[0]
        self.assertEqual("BILI1", first["id"])
        self.assertEqual("https://www.bilibili.com/video/BV1AA1xxxxxx", first["url"])
        self.assertEqual("bilibili.com", first["source_domain"])
        self.assertEqual("2025-05-28", first["date"])
        self.assertEqual("high", first["date_confidence"])
        self.assertIn("长沙麻将", first["title"])
        # Both title and snippet must be free of <em> highlight tags.
        self.assertNotIn("<em", first["title"])
        self.assertNotIn("<em", first["snippet"])
        self.assertIn("长沙麻将", first["snippet"])
        self.assertEqual(124000, first["engagement"]["play"])
        # coin/share are not returned by /search/type, so they must not appear.
        self.assertNotIn("coin", first["engagement"])
        self.assertNotIn("share", first["engagement"])
        self.assertIn("review", first["engagement"])
        self.assertEqual("BV1AA1xxxxxx", first["extra"]["bvid"])
        self.assertEqual("测试UP主A", first["extra"]["author"])
        self.assertGreaterEqual(first["relevance"], 0.05)
        self.assertLessEqual(first["relevance"], 1.0)

    def test_filters_by_date_range(self):
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: _load("search_video_response.json")):
            items = bilibili.search_bilibili(
                topic="麻将",
                from_date="2025-05-15",
                to_date="2025-06-01",
                depth="default",
            )
        self.assertEqual(1, len(items))
        self.assertEqual("2025-05-28", items[0]["date"])

    def test_drops_items_with_no_parseable_date(self):
        """An item whose pubdate can't be parsed must not silently bypass the
        date-window filter — it is dropped."""
        resp = {
            "code": 0,
            "data": {
                "result": [
                    {"type": "video", "bvid": "BV1dated", "title": "dated",
                     "pubdate": 1748390400},  # 2025-05-28, inside window
                    {"type": "video", "bvid": "BV1undated", "title": "undated",
                     "pubdate": 0},  # _pubdate_to_iso -> None
                ]
            },
        }
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: resp):
            items = bilibili.search_bilibili("x", "2025-01-01", "2025-12-31")
        self.assertEqual(1, len(items))
        self.assertEqual("BV1dated", items[0]["extra"]["bvid"])

    def test_respects_depth_caps(self):
        captured = {}

        def fake_get(url, **kw):
            captured["params"] = kw.get("params", {})
            return _load("search_video_response.json")

        with self._patch_session(), patch.object(bilibili.http, "get", fake_get):
            bilibili.search_bilibili("x", "2020-01-01", "2030-01-01", depth="quick")
            self.assertEqual(10, captured["params"]["page_size"])

            bilibili.search_bilibili("x", "2020-01-01", "2030-01-01", depth="default")
            self.assertEqual(20, captured["params"]["page_size"])

            bilibili.search_bilibili("x", "2020-01-01", "2030-01-01", depth="deep")
            self.assertEqual(30, captured["params"]["page_size"])

    def test_raises_on_v_voucher(self):
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: _load("v_voucher_response.json")):
            with self.assertRaises(http.HTTPError) as ctx:
                bilibili.search_bilibili("x", "2020-01-01", "2030-01-01")
        msg = str(ctx.exception)
        self.assertIn("WBI signature may have rotated", msg)

    def test_v_voucher_clears_session_cache(self):
        """A WBI rotation must invalidate the cached mixin_key so the next
        call re-bootstraps instead of replaying the same failure forever."""
        bilibili._mixin_key_cache = "m" * 32
        bilibili._cookie_jar_cache = {"buvid3": "STALE"}
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: _load("v_voucher_response.json")):
            with self.assertRaises(http.HTTPError):
                bilibili.search_bilibili("x", "2020-01-01", "2030-01-01")
        self.assertIsNone(bilibili._mixin_key_cache)
        self.assertIsNone(bilibili._cookie_jar_cache)

    def test_nonzero_code_clears_session_cache(self):
        """Any non-zero search code (e.g. expired buvid3) also clears the cache."""
        bilibili._mixin_key_cache = "m" * 32
        bilibili._cookie_jar_cache = {"buvid3": "STALE"}
        with self._patch_session(), \
             patch.object(bilibili.http, "get",
                          lambda url, **kw: {"code": -412, "message": "blocked", "data": {}}):
            with self.assertRaises(http.HTTPError):
                bilibili.search_bilibili("x", "2020-01-01", "2030-01-01")
        self.assertIsNone(bilibili._mixin_key_cache)
        self.assertIsNone(bilibili._cookie_jar_cache)

    def test_handles_empty_result_list(self):
        with self._patch_session(), \
             patch.object(bilibili.http, "get", lambda url, **kw: {"code": 0, "data": {"result": []}}):
            items = bilibili.search_bilibili("nothing", "2020-01-01", "2030-01-01")
        self.assertEqual([], items)

    def test_signs_request_params(self):
        captured = {}

        def fake_get(url, **kw):
            captured["params"] = kw.get("params", {})
            return _load("search_video_response.json")

        with self._patch_session(), patch.object(bilibili.http, "get", fake_get):
            bilibili.search_bilibili("test", "2020-01-01", "2030-01-01")

        self.assertIn("wts", captured["params"])
        self.assertIn("w_rid", captured["params"])
        self.assertEqual("video", captured["params"]["search_type"])
        self.assertEqual("test", captured["params"]["keyword"])
