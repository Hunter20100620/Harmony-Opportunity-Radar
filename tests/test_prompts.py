# -*- coding: utf-8 -*-
"""prompt 契约测试：占位符齐全、可安全格式化、包含状态/证据/公式/事实规则。

内建默认模板与外部 prompts/*.txt 必须保持一致（同一占位符集合与关键规则）。
"""
import unittest

from radar import prompts


class PromptContractTest(unittest.TestCase):
    def test_all_templates_validate_with_fixture_values(self):
        for name, spec in prompts.PROMPT_TEMPLATES.items():
            text = prompts.get_prompt(name)
            errors = prompts.validate_prompt(name, text)
            self.assertEqual(errors, [], msg=f"{name}: {errors}")

    def test_required_placeholders_present_in_every_template(self):
        for name, spec in prompts.PROMPT_TEMPLATES.items():
            text = prompts.get_prompt(name)
            found = prompts.find_placeholders(text)
            for ph in spec["placeholders"]:
                self.assertIn(ph, found, msg=f"{name} 缺少 {ph}")

    def test_audit_user_has_status_and_evidence_placeholders(self):
        text = prompts.get_prompt("audit_user")
        for ph in ("probe_status", "evidence_grade", "query_stats",
                   "search_errors", "blank_confidence"):
            self.assertIn("{%s}" % ph, text)

    def test_probe_filter_user_has_max_competitors(self):
        text = prompts.get_prompt("probe_filter_user")
        self.assertIn("{max_competitors}", text)

    def test_prompts_state_error_is_not_blank(self):
        audit_system = prompts.get_prompt("audit_system")
        self.assertIn("request_error", audit_system)
        self.assertIn("不是", audit_system)
        probe_filter = prompts.get_prompt("probe_filter_system")
        self.assertIn("空数组", probe_filter)

    def test_formula_ownership_documented(self):
        audit_system = prompts.get_prompt("audit_system")
        self.assertIn("0.25", audit_system)
        self.assertIn("0.30", audit_system)

    def test_external_templates_match_builtin_placeholders(self):
        for name, spec in prompts.PROMPT_TEMPLATES.items():
            path = prompts.prompt_path(name)
            if not path.exists():
                continue
            external = path.read_text(encoding="utf-8")
            found = prompts.find_placeholders(external)
            builtin = prompts.find_placeholders(spec["default"])
            self.assertEqual(found, builtin,
                             msg=f"{name} 外部模板占位符与内建不一致")

    def test_audit_system_has_c_grade_blank_discipline(self):
        audit_system = prompts.get_prompt("audit_system")
        self.assertIn("C 级空白", audit_system)
        self.assertIn("native_advantage", audit_system)
        self.assertIn("indie_feasibility", audit_system)
        self.assertIn("不得同时在多个维度拿到 7 分以上", audit_system)

    def test_external_audit_system_matches_builtin_default(self):
        path = prompts.prompt_path("audit_system")
        if not path.exists():
            self.skipTest("无外部 audit_system 文件")
        external = path.read_text(encoding="utf-8")
        self.assertEqual(external, prompts.PROMPT_TEMPLATES["audit_system"]["default"])


if __name__ == "__main__":
    unittest.main()