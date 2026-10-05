# -*- coding: utf-8 -*-
"""audit 阶段单元测试：门槛过滤、完整审计产物与熔断保护。"""
import unittest
from unittest import mock

from radar.pipeline import PipelineAbortError
from radar.pipeline import audit


def _bm(name, apple_id):
    return {"name": name, "apple_id": apple_id, "artist": "A",
            "jtbd": "J", "pick_reason": "R"}


def _probe(name):
    return {"name": name, "blank_signal": False, "competitors": []}


def _audit_raw(v):
    """四维同值 → 加权 overall 恒等于该值。"""
    return {
        "scores": {"demand": v, "experience_gap": v,
                   "native_advantage": v, "indie_feasibility": v, "overall": v},
        "verdict": "v", "native_features": [], "attack_vector": "a", "indie_advice": "i",
    }


def _fake_llm_factory(responses):
    class _FakeLLM:
        def __init__(self, *args, **kwargs):
            self._i = 0

        def chat_json(self, system, user, **kwargs):
            r = responses[self._i]
            self._i += 1
            return r

    return _FakeLLM


class AuditContractTest(unittest.TestCase):
    def _run(self, cfg, responses, bms, probes):
        bench = {"benchmarks": bms}
        probe = {"benchmarks": probes}
        saved = {}

        def _load(stage):
            return bench if stage == "benchmarks" else probe

        with mock.patch.object(audit, "_load_or_fail", side_effect=_load), \
                mock.patch.object(audit, "LLMClient", _fake_llm_factory(responses)), \
                mock.patch.object(audit.store, "save_opportunities",
                                  side_effect=lambda d: saved.setdefault("opp", d)), \
                mock.patch.object(audit.store, "save_audit_results",
                                  side_effect=lambda d: saved.setdefault("audit_results", d)), \
                mock.patch.object(audit.store, "save_review_queue",
                                  side_effect=lambda d: saved.setdefault("review_queue", d)), \
                mock.patch.object(audit.store, "save_report",
                                  side_effect=lambda m: saved.setdefault("report", m)):
            n = audit.run(cfg)
        return n, saved

    def test_min_score_filters_opportunities_but_preserves_all_audits(self):
        bms = [_bm("A", "1"), _bm("B", "2"), _bm("C", "3")]
        probes = [_probe("A"), _probe("B"), _probe("C")]
        responses = [_audit_raw(8.0), _audit_raw(6.0), _audit_raw(5.9)]
        cfg = {"llm": {"model": "m", "base_url": "http://x"},
               "audit": {"min_score": 6.0}}

        n, saved = self._run(cfg, responses, bms, probes)

        ar = saved["audit_results"]
        self.assertEqual(ar["total_audited"], 3)
        self.assertEqual(ar["total_passed"], 2)
        self.assertEqual(ar["total_rejected"], 1)
        self.assertEqual(len(ar["audits"]), 3)

        opp = saved["opp"]
        overalls = [o["scores"]["overall"] for o in opp["opportunities"]]
        self.assertEqual(overalls, [8.0, 6.0])
        self.assertEqual(opp["total_passed"], 2)
        self.assertEqual(opp["total_rejected"], 1)
        self.assertEqual(n, 2)

    def test_overall_ignores_inconsistent_model_value(self):
        bm = _bm("A", "1")
        raw = {
            "scores": {"demand": 6.2, "experience_gap": 6.2,
                       "native_advantage": 6.2, "indie_feasibility": 6.2,
                       "overall": 10.0},
            "verdict": "v", "native_features": [], "attack_vector": "a",
            "indie_advice": "i",
        }
        normalized = audit._normalize_audit_result(bm, None, raw)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["scores"]["overall"], 6.2)
        self.assertEqual(normalized["model_overall"], 10.0)
        self.assertFalse(normalized["overall_consistent"])

    def test_out_of_range_or_non_finite_dimension_is_invalid(self):
        bm = _bm("A", "1")
        for bad in ({"demand": 11, "experience_gap": 5, "native_advantage": 5,
                     "indie_feasibility": 5},
                    {"demand": float("nan"), "experience_gap": 5,
                     "native_advantage": 5, "indie_feasibility": 5},
                    {"demand": float("inf"), "experience_gap": 5,
                     "native_advantage": 5, "indie_feasibility": 5},
                    {"demand": "x", "experience_gap": 5,
                     "native_advantage": 5, "indie_feasibility": 5},
                    {"demand": True, "experience_gap": 5,
                     "native_advantage": 5, "indie_feasibility": 5}):
            raw = {"scores": bad, "verdict": "v", "native_features": [],
                   "attack_vector": "a", "indie_advice": "i"}
            self.assertIsNone(audit._normalize_audit_result(bm, None, raw),
                              msg=f"should be invalid: {bad}")

    def test_review_queue_includes_error_and_low_confidence(self):
        bms = [_bm("A", "1"), _bm("B", "2")]
        probes = [
            {"name": "A", "blank_signal": False, "probe_status": "request_error",
             "evidence_grade": "D", "competitors": []},
            {"name": "B", "blank_signal": False,
             "probe_status": "confirmed_competitors", "evidence_grade": "A",
             "competitors": [{"name": "C", "score": 4.5, "bad_reviews": []}]},
        ]
        raw_results = [_audit_raw(7.0), _audit_raw(9.0)]
        audits = []
        for bm, pr, raw in zip(bms, probes, raw_results):
            item = audit._normalize_audit_result(bm, pr, raw)
            audits.append(item)

        queue = audit._build_review_queue(audits, probes)
        reasons = {q["apple_id"]: q["reason_codes"] for q in queue}
        self.assertIn("1", reasons)
        self.assertIn("request_error", reasons["1"])
        self.assertNotIn("2", reasons)

    def test_review_queue_includes_near_threshold(self):
        bms = [_bm("N", "9")]
        probes = [{"name": "N", "blank_signal": False,
                   "probe_status": "confirmed_competitors", "evidence_grade": "A",
                   "competitors": []}]
        item = audit._normalize_audit_result(bms[0], probes[0], _audit_raw(5.8))
        queue = audit._build_review_queue([item], probes, min_score=6.0)
        self.assertEqual(len(queue), 1)
        self.assertIn("near_threshold", queue[0]["reason_codes"])

    def test_evidence_cap_limits_gap_for_grade_d(self):
        item = {"scores": {"demand": 9.0, "experience_gap": 9.0,
                           "native_advantage": 9.0, "indie_feasibility": 9.0,
                           "overall": 9.0}}
        probe_record = {"probe_status": "request_error", "evidence_grade": "D"}
        capped = audit._apply_evidence_caps(item, probe_record)
        self.assertEqual(capped["scores"]["experience_gap"], 4.0)
        self.assertTrue(capped["score_cap_applied"])
        self.assertEqual(capped["scores"]["overall"],
                         audit._compute_overall(capped["scores"]))

    def test_evidence_cap_limits_gap_for_grade_c(self):
        item = {"scores": {"demand": 9.0, "experience_gap": 9.0,
                           "native_advantage": 9.0, "indie_feasibility": 9.0,
                           "overall": 9.0}}
        probe_record = {"probe_status": "no_search_results", "evidence_grade": "C"}
        capped = audit._apply_evidence_caps(item, probe_record)
        self.assertEqual(capped["scores"]["experience_gap"], 7.0)
        self.assertTrue(capped["score_cap_applied"])

    def test_evidence_cap_noop_for_grade_a(self):
        item = {"scores": {"demand": 9.0, "experience_gap": 9.0,
                           "native_advantage": 9.0, "indie_feasibility": 9.0,
                           "overall": 9.0}}
        probe_record = {"probe_status": "confirmed_competitors",
                        "evidence_grade": "A"}
        capped = audit._apply_evidence_caps(item, probe_record)
        self.assertEqual(capped["scores"]["experience_gap"], 9.0)
        self.assertFalse(capped.get("score_cap_applied", False))

    def test_all_valid_results_below_threshold_do_not_write_empty_opportunities(self):
        bms = [_bm("A", "1"), _bm("B", "2")]
        probes = [_probe("A"), _probe("B")]
        responses = [_audit_raw(5.0), _audit_raw(4.0)]
        cfg = {"llm": {"model": "m", "base_url": "http://x"},
               "audit": {"min_score": 6.0}}

        with self.assertRaises(PipelineAbortError):
            self._run(cfg, responses, bms, probes)


if __name__ == "__main__":
    unittest.main()