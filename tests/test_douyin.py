import unittest

from lib import douyin, normalize


def _envelope(aweme_infos):
    return {"code": 200, "data": {"data": [{"aweme_info": a} for a in aweme_infos]}}


class TestDouyinParse(unittest.TestCase):
    def _aweme(self, **overrides):
        base = {
            "aweme_id": "7300",
            "desc": "国产大模型实测 #AI",
            "statistics": {
                "play_count": "12.3万",
                "digg_count": 23000,
                "comment_count": 560,
                "share_count": 800,
            },
            "author": {"unique_id": "tech_up", "nickname": "科技UP"},
            "create_time": 1749600000,  # 2025-06-11
            "share_url": "https://www.douyin.com/video/7300?x=1",
            "text_extra": [{"hashtag_name": "AI"}],
        }
        base.update(overrides)
        return base

    def test_unwrap_nested_aweme_info(self):
        awemes = douyin._unwrap_awemes(_envelope([self._aweme()]))
        self.assertEqual(len(awemes), 1)
        self.assertEqual(awemes[0]["aweme_id"], "7300")

    def test_unwrap_flat_aweme_list(self):
        resp = {"code": 200, "data": {"aweme_list": [self._aweme()]}}
        self.assertEqual(len(douyin._unwrap_awemes(resp)), 1)

    def test_parses_chinese_count_suffix(self):
        items = douyin._parse_items([self._aweme()], "大模型")
        self.assertEqual(items[0]["engagement"]["views"], 123000)

    def test_engagement_dimensions(self):
        eng = douyin._parse_items([self._aweme()], "大模型")[0]["engagement"]
        self.assertEqual(eng["likes"], 23000)
        self.assertEqual(eng["comments"], 560)
        self.assertEqual(eng["shares"], 800)

    def test_author_dict_prefers_unique_id(self):
        items = douyin._parse_items([self._aweme()], "x")
        self.assertEqual(items[0]["author_name"], "tech_up")

    def test_author_falls_back_to_nickname(self):
        aweme = self._aweme(author={"nickname": "只有昵称"})
        items = douyin._parse_items([aweme], "x")
        self.assertEqual(items[0]["author_name"], "只有昵称")

    def test_url_strips_query_and_falls_back(self):
        items = douyin._parse_items([self._aweme()], "x")
        self.assertEqual(items[0]["url"], "https://www.douyin.com/video/7300")
        no_url = self._aweme()
        del no_url["share_url"]
        items2 = douyin._parse_items([no_url], "x")
        self.assertEqual(items2[0]["url"], "https://www.douyin.com/video/7300")

    def test_skips_aweme_without_id(self):
        self.assertEqual(douyin._parse_items([{"desc": "no id"}], "x"), [])

    def test_hashtags_extracted(self):
        items = douyin._parse_items([self._aweme()], "x")
        self.assertEqual(items[0]["hashtags"], ["AI"])


class TestDouyinNormalize(unittest.TestCase):
    def test_normalizes_via_shortform(self):
        items = douyin._parse_items(
            [{"aweme_id": "1", "desc": "国产大模型横评", "statistics": {"digg_count": 100},
              "author": {"nickname": "UP"}, "create_time": 1749600000}],
            "大模型",
        )
        norm = normalize.normalize_source_items("douyin", items, "2025-06-01", "2025-06-30")
        self.assertEqual(len(norm), 1)
        self.assertEqual(norm[0].source, "douyin")
        self.assertEqual(norm[0].engagement["likes"], 100)


class TestDouyinComments(unittest.TestCase):
    def test_parse_comments_sorted_by_likes(self):
        comments = [
            {"text": "讲得好", "user": {"nickname": "甲"}, "digg_count": 50, "create_time": 1749600000},
            {"text": "学到了", "user": {"nickname": "乙"}, "digg_count": 300, "create_time": 1749600000},
        ]
        from lib import http
        orig = http.get
        http.get = lambda *a, **k: {"code": 200, "data": {"comments": comments}}
        try:
            out = douyin._fetch_comments("7300", "tok", max_comments=5)
        finally:
            http.get = orig
        self.assertEqual(out[0]["author"], "乙")
        self.assertEqual(out[0]["digg_count"], 300)
        self.assertEqual(len(out), 2)


class TestDouyinNoToken(unittest.TestCase):
    def test_search_without_token_returns_error(self):
        result = douyin.search_douyin("x", "2025-06-01", "2025-06-30", token=None)
        self.assertEqual(result["items"], [])
        self.assertIn("TIKHUB", result["error"])


if __name__ == "__main__":
    unittest.main()
