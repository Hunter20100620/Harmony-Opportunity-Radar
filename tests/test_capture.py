# -*- coding: utf-8 -*-
"""capture 阶段单元测试：截断元数据、主键匹配与 JTBD 置信度。"""
import unittest

from radar.pipeline import capture
from radar import config


class CaptureIdentityTest(unittest.TestCase):
    def _apps(self):
        return [
            {"name": "Same Name", "artist": "A", "apple_id": "111",
             "rank": 1, "url": "u1", "source": "apple/cn/top-free"},
            {"name": "Same Name", "artist": "B", "apple_id": "222",
             "rank": 2, "url": "u2", "source": "apple/us/top-free"},
            {"name": "Only Name", "artist": "C", "apple_id": "",
             "rank": 3, "url": "u3", "source": "apple/cn/top-paid"},
        ]

    def test_apple_id_is_primary_match_key(self):
        apps = self._apps()
        by_id, by_name = capture._build_identity_index(apps)
        origin, match = capture._resolve_identity(
            {"name": "Same Name", "apple_id": "222"}, by_id, by_name)
        self.assertEqual(origin["apple_id"], "222")
        self.assertEqual(match, "apple_id")

    def test_name_fallback_when_apple_id_missing(self):
        apps = self._apps()
        by_id, by_name = capture._build_identity_index(apps)
        origin, match = capture._resolve_identity(
            {"name": "Same Name", "apple_id": "999"}, by_id, by_name)
        self.assertEqual(match, "name_fallback")
        self.assertIsNotNone(origin)

    def test_unknown_identity_returns_none(self):
        apps = self._apps()
        by_id, by_name = capture._build_identity_index(apps)
        origin, match = capture._resolve_identity(
            {"name": "Nope", "apple_id": "999"}, by_id, by_name)
        self.assertIsNone(origin)
        self.assertEqual(match, "unmatched")

    def test_benchmark_key_and_jtbd_confidence(self):
        bm = capture._build_benchmark(
            {"name": "X", "artist": "A", "apple_id": "111", "jtbd": "j",
             "search_keywords": ["k"], "pick_reason": "r"},
            {"url": "u", "rank": 1, "source": "s"},
            "apple_id")
        self.assertEqual(bm["benchmark_key"], "apple:111")
        self.assertEqual(bm["jtbd_confidence"], "name_only")
        self.assertEqual(bm["identity_match"], "apple_id")


class CaptureConfigTest(unittest.TestCase):
    def test_llm_input_limit_default(self):
        self.assertEqual(config.DEFAULTS["capture"]["llm_input_limit"], 200)

    def test_llm_input_limit_range(self):
        cfg = config.load_config()
        cfg["capture"]["llm_input_limit"] = 0
        self.assertTrue(config.validate_config(cfg))
        cfg["capture"]["llm_input_limit"] = 501
        self.assertTrue(config.validate_config(cfg))
        cfg["capture"]["llm_input_limit"] = 200
        self.assertEqual(config.validate_config(cfg), [])


if __name__ == "__main__":
    unittest.main()