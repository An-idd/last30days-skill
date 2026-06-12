import unittest

from lib import bilibili, normalize


def _envelope(videos):
    return {"code": 200, "data": {"result": [{"result_type": "video", "data": videos}]}}


class TestBilibiliParse(unittest.TestCase):
    def _raw(self, **overrides):
        base = {
            "bvid": "BV1xx",
            "title": '国产<em class="keyword">大模型</em>测评',
            "description": "横向对比测试",
            "author": "某UP主",
            "arcurl": "https://www.bilibili.com/video/BV1xx",
            "play": "12.3万",
            "video_review": 3400,
            "favorites": 8900,
            "like": 23000,
            "review": 560,
            "pubdate": 1749600000,  # 2025-06-11
            "tag": "AI,大模型",
        }
        base.update(overrides)
        return base

    def test_strips_em_highlight_tags(self):
        items = bilibili._parse_items([self._raw()], "大模型")
        self.assertEqual(items[0]["title"], "国产大模型测评")

    def test_parses_chinese_count_suffix(self):
        items = bilibili._parse_items([self._raw()], "大模型")
        self.assertEqual(items[0]["engagement"]["views"], 123000)

    def test_engagement_dimensions(self):
        eng = bilibili._parse_items([self._raw()], "大模型")[0]["engagement"]
        self.assertEqual(eng["danmaku"], 3400)
        self.assertEqual(eng["favorites"], 8900)
        self.assertEqual(eng["likes"], 23000)
        self.assertEqual(eng["comments"], 560)

    def test_url_fallback_when_missing(self):
        raw = self._raw()
        del raw["arcurl"]
        items = bilibili._parse_items([raw], "大模型")
        self.assertEqual(items[0]["url"], "https://www.bilibili.com/video/BV1xx")

    def test_skips_entries_without_bvid(self):
        items = bilibili._parse_items([{"title": "no id"}], "x")
        self.assertEqual(items, [])

    def test_extract_video_entries_grouped_shape(self):
        payload = bilibili._unwrap(_envelope([self._raw()]))
        entries = bilibili._extract_video_entries(payload)
        self.assertEqual(len(entries), 1)

    def test_extract_video_entries_flat_shape(self):
        payload = {"result": [self._raw()]}
        self.assertEqual(len(bilibili._extract_video_entries(payload)), 1)


class TestBilibiliNormalize(unittest.TestCase):
    def test_reuses_youtube_normalizer(self):
        items = bilibili._parse_items(
            [{"bvid": "BV1", "title": "测评", "description": "国产大模型横评",
              "author": "UP", "play": 1000, "pubdate": 1749600000}],
            "大模型",
        )
        norm = normalize.normalize_source_items("bilibili", items, "2025-06-01", "2025-06-30")
        self.assertEqual(len(norm), 1)
        self.assertEqual(norm[0].source, "bilibili")
        self.assertEqual(norm[0].title, "测评")
        self.assertEqual(norm[0].engagement["views"], 1000)


class TestBilibiliComments(unittest.TestCase):
    def test_parse_comments_envelope(self):
        replies = [
            {"content": {"message": "讲得很好"}, "member": {"uname": "用户A"}, "like": 99, "ctime": 1749600000},
            {"content": {"message": "学到了"}, "member": {"uname": "用户B"}, "like": 200, "ctime": 1749600000},
        ]
        # Monkeypatch http.get to return the envelope without network.
        from lib import http
        orig = http.get
        http.get = lambda *a, **k: {"code": 200, "data": {"replies": replies}}
        try:
            out = bilibili._fetch_comments("BV1", "tok", max_comments=5)
        finally:
            http.get = orig
        # Sorted by likes desc -> 用户B first.
        self.assertEqual(out[0]["author"], "用户B")
        self.assertEqual(out[0]["likes"], 200)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
