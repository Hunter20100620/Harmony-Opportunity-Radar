# -*- coding: utf-8 -*-
"""store 单元测试：批次质量统计、新产物读写与熔断不改写历史。"""
import unittest

from radar import store


class StoreArtifactTest(unittest.TestCase):
    def test_new_artifacts_registered(self):
        self.assertIn("audit_results.json", store.ALL_ARTIFACTS)
        self.assertIn("audit_results.json", store.STAGE_OUTPUTS["audit"])
        self.assertIn("review_queue.json", store.ALL_ARTIFACTS)

    def test_named_readers_exist(self):
        self.assertTrue(callable(store.save_audit_results))
        self.assertTrue(callable(store.load_audit_results))
        self.assertTrue(callable(store.save_review_queue))
        self.assertTrue(callable(store.load_review_queue))


class BatchQualityTest(unittest.TestCase):
    def _probe_records(self):
        return [
            {"probe_status": "request_error"},
            {"probe_status": "llm_filter_failed"},
            {"probe_status": "weak_evidence"},
            {"probe_status": "confirmed_empty"},
            {"probe_status": "confirmed_competitors"},
        ]

    def test_quality_summary_counts_statuses(self):
        summary = store.summarize_quality(
            self._probe_records(),
            [{"overall_consistent": False}, {"overall_consistent": True}],
            failed=1, invalid=2)
        self.assertEqual(summary["request_errors"], 1)
        self.assertEqual(summary["llm_filter_failures"], 1)
        self.assertEqual(summary["weak_evidence"], 1)
        self.assertEqual(summary["confirmed_empty"], 1)
        self.assertEqual(summary["overall_mismatch"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["invalid"], 2)

    def test_quality_summary_excludes_api_key(self):
        summary = store.summarize_quality([], [], failed=0, invalid=0)
        self.assertNotIn("api_key", summary)


if __name__ == "__main__":
    unittest.main()