# -*- coding: utf-8 -*-
"""故障注入测试：网络/LLM/格式故障时保留历史产物，不写空覆盖。"""
import unittest
from unittest import mock

from radar.pipeline import PipelineAbortError, RetryableAbortError
from radar.pipeline import capture, audit


class CaptureFailureTest(unittest.TestCase):
    def test_all_apple_feeds_fail_is_retryable(self):
        cfg = {"llm": {"model": "m", "base_url": "http://x"},
               "capture": {"regions": ["cn"], "charts": ["top-free"],
                           "feed_limit": 10, "max_benchmarks": 5,
                           "llm_input_limit": 200}}
        with mock.patch.object(capture.apple_rss, "fetch_all", return_value=[]):
            with self.assertRaises(RetryableAbortError):
                capture.run(cfg)

    def test_llm_connection_failure_aborts_without_overwrite(self):
        cfg = {"llm": {"model": "m", "base_url": "http://x"},
               "capture": {"regions": ["cn"], "charts": ["top-free"],
                           "feed_limit": 10, "max_benchmarks": 5,
                           "llm_input_limit": 200}}
        apps = [{"name": "A", "artist": "X", "apple_id": "1", "rank": 1,
                 "url": "u", "source": "apple/cn/top-free"}]
        saved = []

        class BoomLLM:
            def __init__(self, *a, **k):
                pass

            def chat_json(self, *a, **k):
                from radar.llm import LLMError
                raise LLMError("down")

        with mock.patch.object(capture.apple_rss, "fetch_all", return_value=apps), \
                mock.patch.object(capture, "LLMClient", BoomLLM), \
                mock.patch.object(capture.store, "save_raw_feed",
                                  side_effect=lambda d: saved.append("raw")), \
                mock.patch.object(capture.store, "save_benchmarks",
                                  side_effect=lambda d: saved.append("bench")):
            with self.assertRaises(RetryableAbortError):
                capture.run(cfg)
        # 原始快照已落盘（便于重跑），但绝不写空 benchmarks
        self.assertIn("raw", saved)
        self.assertNotIn("bench", saved)


class AuditFailureInjectionTest(unittest.TestCase):
    def _cfg(self):
        return {"llm": {"model": "m", "base_url": "http://x"},
                "audit": {"min_score": 6.0}}

    def _run(self, responses, bms, probes):
        bench = {"benchmarks": bms}
        probe = {"benchmarks": probes}
        saved = {}

        class FakeLLM:
            def __init__(self, *a, **k):
                self._i = 0

            def chat_json(self, *a, **k):
                r = responses[self._i]
                self._i += 1
                if isinstance(r, Exception):
                    raise r
                return r

        with mock.patch.object(audit, "_load_or_fail",
                               side_effect=lambda s: bench if s == "benchmarks" else probe), \
                mock.patch.object(audit, "LLMClient", FakeLLM), \
                mock.patch.object(audit.store, "save_opportunities",
                                  side_effect=lambda d: saved.setdefault("opp", d)), \
                mock.patch.object(audit.store, "save_audit_results",
                                  side_effect=lambda d: saved.setdefault("ar", d)), \
                mock.patch.object(audit.store, "save_review_queue",
                                  side_effect=lambda d: saved.setdefault("rq", d)), \
                mock.patch.object(audit.store, "save_report",
                                  side_effect=lambda m: saved.setdefault("report", m)):
            return audit.run(self._cfg()), saved

    def _bm(self, i):
        return {"name": f"A{i}", "apple_id": str(i), "artist": "A",
                "jtbd": "J", "pick_reason": "R"}

    def _probe(self, i):
        return {"name": f"A{i}", "blank_signal": False, "competitors": []}

    def test_llm_connection_failure_all_aborts(self):
        from radar.llm import LLMError
        bms = [self._bm(1), self._bm(2)]
        probes = [self._probe(1), self._probe(2)]
        with self.assertRaises(PipelineAbortError):
            self._run([LLMError("x"), LLMError("x")], bms, probes)

    def test_malformed_json_does_not_overwrite(self):
        bms = [self._bm(1)]
        probes = [self._probe(1)]
        with self.assertRaises(PipelineAbortError):
            self._run(["not a dict"], bms, probes)

    def test_out_of_range_scores_invalid(self):
        bms = [self._bm(1)]
        probes = [self._probe(1)]
        bad = {"scores": {"demand": 99, "experience_gap": 5,
                          "native_advantage": 5, "indie_feasibility": 5},
               "verdict": "v", "native_features": [], "attack_vector": "a",
               "indie_advice": "i"}
        with self.assertRaises(PipelineAbortError):
            self._run([bad], bms, probes)


class CliExitCodeTest(unittest.TestCase):
    """退出码约定：可重试中断 = 2，致命熔断 = 1，成功 = 0。"""

    def _main_rc(self, run_stage_rc):
        from radar import cli
        with mock.patch.object(cli, "load_config", return_value={}), \
                mock.patch.object(cli, "_run_stage", return_value=run_stage_rc):
            return cli.main(["capture"])

    def test_success_returns_zero(self):
        self.assertEqual(self._main_rc(5), 0)

    def test_fatal_abort_returns_one(self):
        self.assertEqual(self._main_rc(-1), 1)

    def test_retryable_abort_returns_two(self):
        self.assertEqual(self._main_rc(-2), 2)


if __name__ == "__main__":
    unittest.main()