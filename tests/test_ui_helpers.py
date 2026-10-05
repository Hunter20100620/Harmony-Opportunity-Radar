# -*- coding: utf-8 -*-
"""WebUI 数据辅助函数回归：旧记录缺新字段时必须按 legacy/unknown 渲染而非崩溃。

仅测试不依赖 Streamlit 运行时的纯函数。
"""
import unittest

from radar.pipeline import audit


class LegacyRenderTest(unittest.TestCase):
    def test_legacy_probe_record_without_status_gives_unknown(self):
        legacy = {"name": "旧标杆", "blank_signal": True, "competitors": []}
        ctx = audit._build_audit_context({"name": "旧标杆"}, legacy)
        self.assertEqual(ctx["probe_status"], "unknown")
        self.assertEqual(ctx["evidence_grade"], "")
        # 旧记录仍按 blank_signal 渲染为空白，不崩溃
        self.assertEqual(ctx["is_blank"], "是")

    def test_legacy_opportunity_without_new_fields_has_no_grade(self):
        legacy = {"benchmark_name": "X", "scores": {"overall": 7.0},
                  "is_blank": True}
        # 缺 evidence_grade/probe_status 时读取应安全返回默认
        self.assertEqual(legacy.get("evidence_grade", ""), "")
        self.assertEqual(legacy.get("probe_status", ""), "")


if __name__ == "__main__":
    unittest.main()