import json
import unittest
from unittest import mock

from lib import xiaohongshu_api as xhs


def _jsonrpc_result(rid, result):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _tool_result(text):
    return {"content": [{"type": "text", "text": text}]}


SEARCH_PAYLOAD = {
    "feeds": [
        {
            "id": "feed1",
            "xsecToken": "tok1",
            "noteCard": {
                "type": "normal",
                "displayTitle": "三天瘦五斤的减脂餐",
                "user": {"nickname": "健身小王"},
                "interactInfo": {
                    "likedCount": "1.2万",
                    "commentCount": "320",
                    "collectedCount": "5000",
                },
            },
        },
        {
            "id": "feed2",
            "xsecToken": "tok2",
            "noteCard": {
                "type": "video",
                "displayTitle": "AI 工具实测",
                "interactInfo": {"likedCount": "88", "commentCount": "3", "collectedCount": "10"},
            },
        },
        "junk-not-a-dict",
    ],
    "count": 2,
}


def _make_fake_post(login_text="✅ 已登录\n用户名: tester", search_payload=SEARCH_PAYLOAD,
                    search_is_error=False):
    """Return a side_effect fn for _raw_post that mimics the MCP server."""
    state = {"rid": 0}

    def fake_post(url, payload, headers, timeout):
        method = payload.get("method")
        if method == "initialize":
            # session id handed back on initialize, echoed thereafter.
            return _jsonrpc_result(payload["id"], {"protocolVersion": "2025-06-18"}), "sess-xyz"
        if method == "notifications/initialized":
            return "", None
        if method == "tools/call":
            name = payload["params"]["name"]
            # Subsequent requests must carry the session header.
            assert headers.get("Mcp-Session-Id") == "sess-xyz", "session header not echoed"
            if name == "check_login_status":
                return _jsonrpc_result(payload["id"], _tool_result(login_text)), None
            if name == "search_feeds":
                res = _tool_result(json.dumps(search_payload))
                if search_is_error:
                    res["isError"] = True
                return _jsonrpc_result(payload["id"], res), None
        raise AssertionError(f"unexpected call: {method}")

    return fake_post


class TestSearchFeeds(unittest.TestCase):
    def test_parses_feeds_into_web_items(self):
        with mock.patch.object(xhs, "_raw_post", side_effect=_make_fake_post()):
            items = xhs.search_feeds("减脂餐", "2026-01-01", "2026-06-16",
                                     "http://localhost:18060", depth="default")
        self.assertEqual(2, len(items))
        first = items[0]
        self.assertEqual("XHS1", first["id"])
        self.assertEqual("三天瘦五斤的减脂餐", first["title"])
        self.assertEqual("xiaohongshu.com", first["source_domain"])
        self.assertIn("xsec_token=tok1", first["url"])
        self.assertIn("feed1", first["url"])
        # 1.2万 -> 12000, with engagement preserved
        self.assertEqual(12000, first["engagement"]["likes"])
        self.assertEqual(320, first["engagement"]["comments"])
        self.assertEqual(5000, first["engagement"]["favorites"])
        # No timestamp in MCP search results -> dateless, low confidence
        self.assertIsNone(first["date"])
        self.assertEqual("low", first["date_confidence"])

    def test_depth_caps_results(self):
        big = {"feeds": [{"id": f"f{i}", "noteCard": {"displayTitle": f"t{i}"}} for i in range(40)]}
        with mock.patch.object(xhs, "_raw_post", side_effect=_make_fake_post(search_payload=big)):
            items = xhs.search_feeds("x", "a", "b", "http://h:18060", depth="quick")
        self.assertEqual(8, len(items))  # quick cap

    def test_not_logged_in_raises(self):
        fake = _make_fake_post(login_text="❌ 未登录\n\n请使用 get_login_qrcode ...")
        with mock.patch.object(xhs, "_raw_post", side_effect=fake):
            with self.assertRaises(xhs.http.HTTPError):
                xhs.search_feeds("x", "a", "b", "http://h:18060")

    def test_tool_error_raises(self):
        fake = _make_fake_post(search_is_error=True)
        with mock.patch.object(xhs, "_raw_post", side_effect=fake):
            with self.assertRaises(xhs.http.HTTPError):
                xhs.search_feeds("x", "a", "b", "http://h:18060")

    def test_missing_base_raises(self):
        with self.assertRaises(ValueError):
            xhs.search_feeds("x", "a", "b", "")


class TestCheckLogin(unittest.TestCase):
    def test_logged_in_true(self):
        with mock.patch.object(xhs, "_raw_post", side_effect=_make_fake_post()):
            self.assertTrue(xhs.check_login("http://localhost:18060"))

    def test_logged_out_false(self):
        with mock.patch.object(xhs, "_raw_post", side_effect=_make_fake_post(login_text="❌ 未登录")):
            self.assertFalse(xhs.check_login("http://localhost:18060"))

    def test_unreachable_false(self):
        import urllib.error
        with mock.patch.object(xhs, "_raw_post", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(xhs.check_login("http://localhost:18060"))

    def test_empty_base_false(self):
        self.assertFalse(xhs.check_login(""))


class TestBodyParsing(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual({"a": 1}, xhs._parse_mcp_body('{"a": 1}'))

    def test_sse_framed(self):
        body = "event: message\ndata: {\"a\": 2}\n\n"
        self.assertEqual({"a": 2}, xhs._parse_mcp_body(body))

    def test_empty(self):
        self.assertEqual({}, xhs._parse_mcp_body(""))

    def test_tool_text_extraction(self):
        self.assertEqual("hi", xhs._tool_text({"content": [{"type": "text", "text": "hi"}]}))
        self.assertEqual("", xhs._tool_text({"content": []}))


if __name__ == "__main__":
    unittest.main()
