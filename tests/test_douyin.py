import unittest
from unittest import mock

from lib import douyin, normalize
from lib.douyin import _parse_items, _publish_time, parse_douyin_response


def _raw(**overrides):
    base = {
        "id": "v123",
        "text": "抖音 AI agents 视频",
        "url": "https://www.douyin.com/video/v123?from=search",
        "shareUrl": "https://v.douyin.com/abc/",
        "createTime": 1747526400,  # 2025-05-18 (UTC)
        "authorMeta": {"username": "creator_a", "name": "Creator A"},
        "statistics": {
            "playCount": 50000,
            "diggCount": 3200,
            "commentCount": 410,
            "shareCount": 88,
        },
        "hashtags": ["AI", {"name": "#agents"}],
    }
    base.update(overrides)
    return base


class TestDouyinParse(unittest.TestCase):
    def test_basic_fields(self):
        item = _parse_items([_raw()], "ai agents")[0]
        self.assertEqual("v123", item["id"])
        self.assertEqual("抖音 AI agents 视频", item["text"])
        # Query string stripped from URL.
        self.assertEqual("https://www.douyin.com/video/v123", item["url"])
        # Douyin: nickname (name) is preferred over the numeric username.
        self.assertEqual("Creator A", item["author_name"])
        self.assertEqual(50000, item["engagement"]["views"])
        self.assertEqual(3200, item["engagement"]["likes"])
        self.assertEqual(410, item["engagement"]["comments"])
        self.assertEqual(88, item["engagement"]["shares"])

    def test_hashtags_mixed_string_and_dict(self):
        item = _parse_items([_raw()], "ai agents")[0]
        self.assertEqual(["AI", "agents"], item["hashtags"])

    def test_author_as_string(self):
        item = _parse_items([_raw(authorMeta="plain_handle")], "x")[0]
        self.assertEqual("plain_handle", item["author_name"])

    def test_author_missing(self):
        raw = _raw()
        del raw["authorMeta"]
        item = _parse_items([raw], "x")[0]
        self.assertEqual("", item["author_name"])

    def test_zero_stats_preserved(self):
        raw = _raw(statistics={"playCount": 0, "diggCount": 0, "commentCount": 0, "shareCount": 0})
        item = _parse_items([raw], "x")[0]
        self.assertEqual(0, item["engagement"]["views"])
        self.assertEqual(0, item["engagement"]["likes"])

    def test_stats_missing(self):
        raw = _raw()
        del raw["statistics"]
        item = _parse_items([raw], "x")[0]
        self.assertEqual(0, item["engagement"]["views"])

    def test_url_falls_back_to_share_url(self):
        raw = _raw(url="")
        item = _parse_items([raw], "x")[0]
        self.assertEqual("https://v.douyin.com/abc/", item["url"])

    def test_text_falls_back_to_caption(self):
        raw = _raw(text="", caption="caption text")
        item = _parse_items([raw], "x")[0]
        self.assertEqual("caption text", item["text"])

    def test_date_from_create_date_string(self):
        raw = _raw()
        del raw["createTime"]
        raw["createDate"] = "2025-05-18 12:00:00"
        item = _parse_items([raw], "x")[0]
        self.assertEqual("2025-05-18", item["date"])

    def test_non_dict_items_skipped(self):
        items = _parse_items([_raw(), "junk", None], "x")
        self.assertEqual(1, len(items))

    def test_collects_captured(self):
        raw = _raw()
        raw["statistics"]["collectCount"] = 214
        item = _parse_items([raw], "x")[0]
        self.assertEqual(214, item["engagement"]["collects"])

    def test_ranks_by_likes_when_views_zero(self):
        # Douyin search hides view counts (playCount: 0); ranking must fall back
        # to likes/comments/collects so results aren't left unsorted.
        low = _raw(id="low", statistics={"playCount": 0, "diggCount": 10, "commentCount": 0, "shareCount": 0})
        high = _raw(id="high", statistics={"playCount": 0, "diggCount": 999, "commentCount": 0, "shareCount": 0})
        with mock.patch.object(douyin.http, "post", return_value=[low, high]):
            result = douyin.search_douyin("x", "2025-05-01", "2025-06-01", token="t")
        # high (999 likes) sorts before low despite both having 0 views.
        self.assertEqual("high", result["items"][0]["id"])


class TestPublishTimeBuckets(unittest.TestCase):
    def test_one_day(self):
        self.assertEqual("one_day", _publish_time("2026-06-15", "2026-06-16"))

    def test_one_week(self):
        self.assertEqual("one_week", _publish_time("2026-06-10", "2026-06-16"))

    def test_half_year_for_30_days(self):
        self.assertEqual("half_year", _publish_time("2026-05-17", "2026-06-16"))

    def test_unlimited_for_long_window(self):
        self.assertEqual("unlimited", _publish_time("2025-06-16", "2026-06-16"))

    def test_invalid_dates_default_half_year(self):
        self.assertEqual("half_year", _publish_time("not-a-date", "2026-06-16"))


class TestSearchAndEnrich(unittest.TestCase):
    def test_no_token_returns_error(self):
        result = douyin.search_douyin("topic", "2026-05-17", "2026-06-16", token=None)
        self.assertEqual([], result["items"])
        self.assertIn("APIFY_API_TOKEN", result["error"])

    def test_search_and_enrich_merges_and_snippets(self):
        in_range = _raw(createTime=1748908800)  # 2025-06-03, inside window below

        def fake_post(url, **kwargs):
            self.assertIn("run-sync-get-dataset-items", url)
            self.assertEqual("tok", kwargs["params"]["token"])
            return [in_range]

        with mock.patch.object(douyin.http, "post", side_effect=fake_post):
            result = douyin.search_and_enrich(
                "ai agents", "2025-05-17", "2025-06-16", depth="quick", token="tok"
            )
        items = parse_douyin_response(result)
        self.assertEqual(1, len(items))
        self.assertTrue(items[0]["caption_snippet"])

    def test_dict_response_shape_supported(self):
        with mock.patch.object(
            douyin.http, "post", return_value={"items": [_raw(createTime=1748908800)]}
        ):
            result = douyin.search_douyin(
                "ai agents", "2025-05-17", "2025-06-16", depth="quick", token="tok"
            )
        self.assertEqual(1, len(result["items"]))


class TestCommentEnrichment(unittest.TestCase):
    def _items(self):
        return [
            {"id": "v1", "url": "https://www.douyin.com/video/v1",
             "engagement": {"likes": 100, "comments": 5, "collects": 10}},
            {"id": "v2", "url": "https://www.douyin.com/video/v2",
             "engagement": {"likes": 999, "comments": 5, "collects": 10}},
        ]

    def _rows(self):
        return [
            {"awemeId": "v2", "text": "二条的高赞", "likeCount": 500,
             "createDate": "2026-05-19", "user": {"nickname": "网友A"}},
            {"awemeId": "v2", "text": "二条的次赞", "likeCount": 50,
             "createDate": "2026-05-18", "user": {"nickname": "网友B"}},
            {"awemeId": "v1", "text": "一条的评论", "likeCount": 30,
             "createDate": "2026-05-17", "user": {"nickname": "网友C"}},
        ]

    def test_single_run_with_all_top_urls(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["awemeUrls"] = kwargs["json_data"]["awemeUrls"]
            captured["maxPer"] = kwargs["json_data"]["maxCommentsPerAweme"]
            captured["replies"] = kwargs["json_data"]["includeReplies"]
            return self._rows()

        with mock.patch.object(douyin.http, "post", side_effect=fake_post):
            items = douyin.enrich_with_comments(self._items(), token="t", max_posts=3, max_comments=10)

        # One run, comments actor, both video URLs passed together, no replies.
        self.assertIn("douyin-comments-scraper", captured["url"])
        self.assertEqual(2, len(captured["awemeUrls"]))
        self.assertEqual(10, captured["maxPer"])
        self.assertFalse(captured["replies"])

        by_id = {i["id"]: i for i in items}
        # Grouped by awemeId and sorted by likeCount desc.
        self.assertEqual(2, len(by_id["v2"]["top_comments"]))
        self.assertEqual("二条的高赞", by_id["v2"]["top_comments"][0]["text"])
        self.assertEqual(500, by_id["v2"]["top_comments"][0]["digg_count"])
        self.assertEqual("网友A", by_id["v2"]["top_comments"][0]["author"])
        self.assertEqual("一条的评论", by_id["v1"]["top_comments"][0]["text"])

    def test_respects_max_posts(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["awemeUrls"] = kwargs["json_data"]["awemeUrls"]
            return []

        with mock.patch.object(douyin.http, "post", side_effect=fake_post):
            douyin.enrich_with_comments(self._items(), token="t", max_posts=1)
        # Only the highest-engagement video (v2) is enriched.
        self.assertEqual(["https://www.douyin.com/video/v2"], captured["awemeUrls"])

    def test_no_token_is_noop(self):
        items = douyin.enrich_with_comments(self._items(), token="", max_posts=3)
        self.assertNotIn("top_comments", items[0])

    def test_fetch_error_is_noop(self):
        with mock.patch.object(douyin.http, "post", side_effect=RuntimeError("boom")):
            items = douyin.enrich_with_comments(self._items(), token="t")
        self.assertFalse(any("top_comments" in i for i in items))

    def test_comments_flow_into_normalized_metadata(self):
        items = self._items()
        with mock.patch.object(douyin.http, "post", return_value=self._rows()):
            douyin.enrich_with_comments(items, token="t")
        # date defaults to high for shortform; give the items a date in-range.
        for it in items:
            it.update({"text": "x", "date": "2026-05-19", "hashtags": []})
        normalized = normalize.normalize_source_items("douyin", items, "2026-05-01", "2026-06-01")
        v2 = next(si for si in normalized if si.item_id == "v2")
        self.assertEqual("二条的高赞", v2.metadata["top_comments"][0]["excerpt"])
        self.assertEqual(500, v2.metadata["top_comments"][0]["score"])


class TestCommentsAvailability(unittest.TestCase):
    def test_default_on_with_token(self):
        from lib import env
        self.assertTrue(env.is_douyin_comments_available({"APIFY_API_TOKEN": "x"}))

    def test_off_without_token(self):
        from lib import env
        self.assertFalse(env.is_douyin_comments_available({}))

    def test_suppressed_by_exclude_sources(self):
        from lib import env
        cfg = {"APIFY_API_TOKEN": "x", "EXCLUDE_SOURCES": "douyin_comments"}
        self.assertFalse(env.is_douyin_comments_available(cfg))


class TestDouyinNormalization(unittest.TestCase):
    def test_normalizes_through_shortform_path(self):
        parsed = _parse_items([_raw(createTime=1748908800)], "ai agents")
        items = normalize.normalize_source_items(
            "douyin", parsed, "2025-05-17", "2025-06-16"
        )
        self.assertEqual(1, len(items))
        si = items[0]
        self.assertEqual("douyin", si.source)
        self.assertTrue(si.item_id.startswith("v123") or si.item_id.startswith("DY"))
        self.assertEqual("Creator A", si.author)
        self.assertEqual(50000, si.engagement["views"])
        self.assertIn("agents", si.metadata["hashtags"])


if __name__ == "__main__":
    unittest.main()
