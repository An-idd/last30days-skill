import unittest

from lib import cjk, dedupe, relevance


class TestCjkSegment(unittest.TestCase):
    def test_has_cjk(self):
        self.assertTrue(cjk.has_cjk("国产大模型"))
        self.assertTrue(cjk.has_cjk("GPT4很强"))
        self.assertFalse(cjk.has_cjk("hello world"))
        self.assertFalse(cjk.has_cjk(""))

    def test_ascii_path_unchanged(self):
        # Non-CJK text keeps whitespace/word tokenization.
        self.assertEqual(cjk.segment("best react hooks"), ["best", "react", "hooks"])

    def test_chinese_bigrams(self):
        # Without jieba installed, CJK runs become character bigrams.
        toks = cjk.segment("大模型")
        self.assertIn("大模", toks)
        self.assertIn("模型", toks)

    def test_mixed_language(self):
        toks = cjk.segment("GPT4很强 react")
        self.assertIn("gpt4", toks)
        self.assertIn("react", toks)
        self.assertIn("很强", toks)

    def test_single_cjk_char(self):
        self.assertEqual(cjk.segment("中"), ["中"])


class TestChineseRelevance(unittest.TestCase):
    def test_chinese_query_matches_chinese_text(self):
        q = relevance.PreparedQuery("国产大模型 测评")
        score = relevance.token_overlap_relevance(q, "这是国产大模型的最新测评")
        self.assertGreater(score, 0.5)

    def test_chinese_query_rejects_unrelated_text(self):
        q = relevance.PreparedQuery("国产大模型 测评")
        score = relevance.token_overlap_relevance(q, "今天天气很好适合出门散步")
        self.assertEqual(score, 0.0)

    def test_english_relevance_not_regressed(self):
        q = relevance.PreparedQuery("react hooks")
        self.assertGreaterEqual(relevance.token_overlap_relevance(q, "a guide to react hooks"), 0.9)


class TestChineseDedupe(unittest.TestCase):
    def test_reordered_chinese_is_near_duplicate(self):
        sim = dedupe.hybrid_similarity("国产大模型最新测评对比", "国产大模型测评对比最新")
        self.assertGreater(sim, 0.5)

    def test_distinct_chinese_is_not_duplicate(self):
        sim = dedupe.hybrid_similarity("国产大模型测评", "今天天气很好出门散步")
        self.assertLess(sim, 0.3)


if __name__ == "__main__":
    unittest.main()
