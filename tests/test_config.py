# -*- coding: utf-8 -*-
"""config 单元测试：新增召回配置的默认值与范围校验。"""
import unittest

from radar import config


class ConfigTest(unittest.TestCase):
    def test_new_probe_defaults_exist(self):
        d = config.DEFAULTS["probe"]
        self.assertEqual(d["max_keywords_per_benchmark"], 4)
        self.assertEqual(d["comment_pages"], 4)
        self.assertEqual(d["max_competitors"], 3)

    def test_valid_values_pass_validation(self):
        cfg = config.load_config()
        cfg["probe"]["max_keywords_per_benchmark"] = 8
        cfg["probe"]["comment_pages"] = 20
        cfg["probe"]["max_competitors"] = 20
        self.assertEqual(config.validate_config(cfg), [])

    def test_zero_or_negative_values_fail(self):
        cfg = config.load_config()
        for key, val in (("max_keywords_per_benchmark", 0),
                         ("comment_pages", 0),
                         ("max_competitors", 0),
                         ("max_keywords_per_benchmark", 9),
                         ("comment_pages", 21),
                         ("max_competitors", 21)):
            bad = config.load_config()
            bad["probe"][key] = val
            self.assertTrue(config.validate_config(bad),
                            msg=f"{key}={val} should fail")

    def test_max_competitors_default_in_range(self):
        errors = config.validate_config(config.load_config())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()