"""Tests for skills/last30days/scripts/lib/bilibili_wbi.py."""

import hashlib
import json
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from lib import bilibili_wbi

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bilibili"


def _load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestMixinKeyEncTab(unittest.TestCase):
    def test_length_is_64(self):
        self.assertEqual(64, len(bilibili_wbi.MIXIN_KEY_ENC_TAB))

    def test_values_unique(self):
        self.assertEqual(64, len(set(bilibili_wbi.MIXIN_KEY_ENC_TAB)))

    def test_values_in_range(self):
        self.assertTrue(all(0 <= v < 64 for v in bilibili_wbi.MIXIN_KEY_ENC_TAB))


class TestGetWbiKeys(unittest.TestCase):
    def test_extracts_from_nav_response(self):
        nav = _load_fixture("nav_response.json")
        img_key, sub_key = bilibili_wbi.get_wbi_keys(nav)
        self.assertEqual("7cd084941338484aae1ad9425b84077c", img_key)
        self.assertEqual("4932caff0ff746eab6f01bf08b70ac45", sub_key)

    def test_raises_on_missing_field(self):
        with self.assertRaises(ValueError):
            bilibili_wbi.get_wbi_keys({"code": 0, "data": {}})

    def test_raises_on_malformed_url(self):
        bad = {"data": {"wbi_img": {"img_url": "https://example.com/", "sub_url": "https://example.com/"}}}
        with self.assertRaises(ValueError):
            bilibili_wbi.get_wbi_keys(bad)


class TestMixKey(unittest.TestCase):
    def test_returns_32_chars(self):
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        mixin = bilibili_wbi.mix_key(img_key, sub_key)
        self.assertEqual(32, len(mixin))

    def test_uses_permutation_table(self):
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        combined = img_key + sub_key
        mixin = bilibili_wbi.mix_key(img_key, sub_key)
        for i, src_pos in enumerate(bilibili_wbi.MIXIN_KEY_ENC_TAB[:32]):
            self.assertEqual(combined[src_pos], mixin[i], f"position {i} should map from {src_pos}")


class TestSignParams(unittest.TestCase):
    def test_adds_wts_and_w_rid(self):
        signed = bilibili_wbi.sign_params({"foo": "bar"}, mixin_key="a" * 32, ts=1702204169)
        self.assertEqual(1702204169, signed["wts"])
        self.assertIn("w_rid", signed)
        self.assertEqual(32, len(signed["w_rid"]))

    def test_does_not_mutate_input(self):
        original = {"foo": "bar"}
        bilibili_wbi.sign_params(original, mixin_key="a" * 32, ts=1)
        self.assertEqual({"foo": "bar"}, original)
        self.assertNotIn("wts", original)

    def test_default_ts_is_now(self):
        with patch.object(time, "time", return_value=1700000000.5):
            signed = bilibili_wbi.sign_params({"x": "1"}, mixin_key="a" * 32)
        self.assertEqual(1700000000, signed["wts"])

    def test_w_rid_is_deterministic(self):
        a = bilibili_wbi.sign_params({"k": "v"}, mixin_key="m" * 32, ts=42)
        b = bilibili_wbi.sign_params({"k": "v"}, mixin_key="m" * 32, ts=42)
        self.assertEqual(a["w_rid"], b["w_rid"])

    def test_w_rid_matches_md5_of_sorted_query_plus_key(self):
        # Clean alphanumeric values: encodeURIComponent and urlencode agree,
        # so this also confirms the new canonicalization is backward-compatible.
        params = {"foo": "114", "bar": "514", "baz": "1919810"}
        mixin = "ea1db124af3c7062474693fa704f4ff8"
        ts = 1702204169

        expected_query = urllib.parse.urlencode(sorted({**params, "wts": ts}.items()))
        expected_w_rid = hashlib.md5((expected_query + mixin).encode("utf-8")).hexdigest()

        signed = bilibili_wbi.sign_params(params, mixin_key=mixin, ts=ts)
        self.assertEqual(expected_w_rid, signed["w_rid"])

    def test_strips_wbi_reserved_chars_and_encodes_space_as_pct20(self):
        # "C++ (anime)!" -> strip ()! -> "C++ anime" -> encodeURIComponent
        # -> "C%2B%2B%20anime" (space is %20, not +).
        params = {"keyword": "C++ (anime)!"}
        mixin = "ea1db124af3c7062474693fa704f4ff8"
        ts = 1702204169

        expected_query = "keyword=C%2B%2B%20anime&wts=1702204169"
        expected_w_rid = hashlib.md5((expected_query + mixin).encode("utf-8")).hexdigest()

        signed = bilibili_wbi.sign_params(params, mixin_key=mixin, ts=ts)
        self.assertEqual(expected_w_rid, signed["w_rid"])
        # Original value is preserved in the returned params (only signing strips).
        self.assertEqual("C++ (anime)!", signed["keyword"])

    def test_reserved_chars_change_the_signature(self):
        # Proves the strip/%20 fix actually matters: the naive quote_plus
        # encoding (no strip, space -> +) yields a different, wrong w_rid.
        params = {"keyword": "Yay! (x)"}
        mixin = "m" * 32
        ts = 1

        naive_query = urllib.parse.urlencode(sorted({**params, "wts": ts}.items()))
        naive_w_rid = hashlib.md5((naive_query + mixin).encode("utf-8")).hexdigest()

        signed = bilibili_wbi.sign_params(params, mixin_key=mixin, ts=ts)
        self.assertNotEqual(naive_w_rid, signed["w_rid"])


if __name__ == "__main__":
    unittest.main()
