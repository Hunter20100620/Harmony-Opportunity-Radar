# -*- coding: utf-8 -*-
"""probe 阶段单元测试：请求失败与生态空白的区分、状态机与审计上下文。"""
import unittest

from radar.pipeline import probe
from radar.pipeline import audit
from radar.sources.appgallery import AppGalleryError


def _candidate(pkg, name="C", score=4.5):
    return {"name": name, "appid": pkg, "package": pkg, "score": score,
            "downloads": "10万次安装", "category": "工具", "intro": ""}


class FakeClient:
    def __init__(self, results=None, errors=None, comments=None):
        self.results = results or {}
        self.errors = errors or {}
        self._comments = comments or {}

    def search(self, keyword, max_results=25):
        if keyword in self.errors:
            raise AppGalleryError(self.errors[keyword])
        return self.results.get(keyword, [])

    def detail(self, appid):
        return {}

    def comments(self, appid, page_num=1, page_size=25):
        return self._comments.get(appid, {"count": 0, "total_pages": 0,
                                          "list": [], "rating_dist": []})

    def close(self):
        pass


class FakeLLM:
    def __init__(self, result):
        self.result = result

    def chat_json(self, *args, **kwargs):
        return self.result


def _bm(keywords):
    return {"name": "标杆", "apple_id": "1", "jtbd": "当我X时我想要Y",
            "search_keywords": keywords}


class ProbeStatusTest(unittest.TestCase):
    def test_appgallery_request_error_is_not_blank(self):
        client = FakeClient(errors={"kw1": "boom", "kw2": "boom"})
        rec = probe._probe_one_benchmark(
            client, None, _bm(["kw1", "kw2"]), ["kw1", "kw2"], 50, 3, 8)
        self.assertEqual(rec["probe_status"], "request_error")
        self.assertFalse(rec["blank_signal"])
        self.assertEqual(rec["competitors"], [])
        self.assertEqual(rec["query_stats"]["failed"], 2)

    def test_successful_empty_search_is_no_search_results(self):
        client = FakeClient(results={"kw1": [], "kw2": []})
        rec = probe._probe_one_benchmark(
            client, None, _bm(["kw1", "kw2"]), ["kw1", "kw2"], 50, 3, 8)
        self.assertEqual(rec["probe_status"], "no_search_results")
        self.assertFalse(rec["blank_signal"])
        self.assertEqual(rec["query_stats"]["succeeded"], 2)

    def test_confirmed_empty_requires_successful_search_and_empty_llm_result(self):
        client = FakeClient(results={"kw1": [_candidate("pkg.a")]})
        llm = FakeLLM([])  # LLM 明确返回空数组
        rec = probe._probe_one_benchmark(
            client, llm, _bm(["kw1"]), ["kw1"], 50, 3, 8)
        self.assertEqual(rec["probe_status"], "confirmed_empty")
        self.assertTrue(rec["blank_signal"])

    def test_llm_filter_failure_is_not_confirmed_empty(self):
        client = FakeClient(results={"kw1": [_candidate("pkg.a")]})

        class BoomLLM:
            def chat_json(self, *a, **k):
                from radar.llm import LLMError
                raise LLMError("down")

        rec = probe._probe_one_benchmark(
            client, BoomLLM(), _bm(["kw1"]), ["kw1"], 50, 3, 8)
        self.assertNotEqual(rec["probe_status"], "confirmed_empty")
        self.assertFalse(rec["blank_signal"])

    def test_filter_confirmed_returns_valid_packages(self):
        cands = [_candidate("pkg.a"), _candidate("pkg.b")]
        res = probe._llm_filter_competitors(FakeLLM(["pkg.a"]), "n", "j", cands)
        self.assertEqual(res["status"], "confirmed")
        self.assertEqual(res["packages"], ["pkg.a"])

    def test_filter_valid_empty_array_is_confirmed_empty(self):
        cands = [_candidate("pkg.a")]
        res = probe._llm_filter_competitors(FakeLLM([]), "n", "j", cands)
        self.assertEqual(res["status"], "confirmed_empty")
        self.assertEqual(res["packages"], [])

    def test_filter_malformed_object_is_invalid_response(self):
        cands = [_candidate("pkg.a")]
        res = probe._llm_filter_competitors(FakeLLM({"foo": "bar"}), "n", "j", cands)
        self.assertEqual(res["status"], "invalid_response")

    def test_filter_out_of_list_package_is_invalid_response(self):
        cands = [_candidate("pkg.a")]
        res = probe._llm_filter_competitors(FakeLLM(["pkg.zzz"]), "n", "j", cands)
        self.assertEqual(res["status"], "invalid_response")
        self.assertEqual(res["packages"], [])

    def test_filter_llm_error_is_request_error(self):
        class BoomLLM:
            def chat_json(self, *a, **k):
                from radar.llm import LLMError
                raise LLMError("down")

        res = probe._llm_filter_competitors(BoomLLM(), "n", "j", [_candidate("pkg.a")])
        self.assertEqual(res["status"], "request_error")

    def test_max_competitors_limits_runtime_output(self):
        cands = [_candidate(f"pkg.{i}", name=f"C{i}") for i in range(6)]
        client = FakeClient(results={"kw1": cands})
        llm = FakeLLM([f"pkg.{i}" for i in range(6)])
        rec = probe._probe_one_benchmark(
            client, llm, _bm(["kw1"]), ["kw1"], 50, 3, 8, max_competitors=2)
        self.assertLessEqual(rec["competitor_count"], 2)

    def test_keyword_selection_records_skipped(self):
        used, skipped = probe._select_keywords(["a", "b", "a", "", "c", "d", "e"], 3)
        self.assertEqual(used, ["a", "b", "c"])
        self.assertEqual(skipped, ["d", "e"])

    def test_comment_request_failure_is_not_no_pain(self):
        cands = [_candidate("pkg.a")]
        client = FakeClient(results={"kw1": cands})

        class FailComments(FakeClient):
            def comments(self, appid, page_num=1, page_size=25):
                raise AppGalleryError("comments down")

        client = FailComments(results={"kw1": cands})
        llm = FakeLLM(["pkg.a"])
        rec = probe._probe_one_benchmark(
            client, llm, _bm(["kw1"]), ["kw1"], 50, 3, 8, comment_pages=4)
        comp = rec["competitors"][0]
        self.assertEqual(comp["review_evidence"], "request_error")
        self.assertEqual(comp["comment_stats"]["fetched"], 0)
        self.assertEqual(comp["comment_stats"]["bad_rating"], 0)

    def test_paged_comment_collection_respects_pages(self):
        cands = [_candidate("pkg.a")]
        pages = {
            1: {"count": 2, "total_pages": 5, "list": [
                {"content": "a", "rating": 5, "time": "t", "version": "v"},
                {"content": "b", "rating": 1, "time": "t", "version": "v"}]},
            2: {"count": 2, "total_pages": 5, "list": [
                {"content": "c", "rating": 2, "time": "t", "version": "v"}]},
            3: {"count": 0, "total_pages": 5, "list": []},
        }
        seen_pages = []

        class PagedClient(FakeClient):
            def comments(self, appid, page_num=1, page_size=25):
                seen_pages.append(page_num)
                return pages.get(page_num, {"count": 0, "total_pages": 5,
                                            "list": []})

        client = PagedClient(results={"kw1": cands})
        llm = FakeLLM(["pkg.a"])
        rec = probe._probe_one_benchmark(
            client, llm, _bm(["kw1"]), ["kw1"], 50, 3, 8, comment_pages=4)
        comp = rec["competitors"][0]
        self.assertGreaterEqual(len(seen_pages), 2)
        self.assertLessEqual(len(seen_pages), 4)
        cs = comp["comment_stats"]
        self.assertEqual(cs["fetched"], 3)
        self.assertEqual(cs["valid_rating"], 3)
        self.assertEqual(cs["bad_rating"], 2)
        self.assertEqual(comp["review_evidence"], "sufficient")
        self.assertIn("rating_distribution", cs)

    def test_grade_d_for_request_error(self):
        rec = {"probe_status": "request_error", "query_stats": {"succeeded": 0},
               "competitors": []}
        self.assertEqual(probe._derive_evidence_grade(rec), "D")

    def test_grade_d_for_llm_filter_failed(self):
        rec = {"probe_status": "llm_filter_failed",
               "query_stats": {"succeeded": 2}, "competitors": []}
        self.assertEqual(probe._derive_evidence_grade(rec), "D")

    def test_grade_c_for_no_search_results(self):
        rec = {"probe_status": "no_search_results",
               "query_stats": {"succeeded": 1}, "competitors": []}
        self.assertEqual(probe._derive_evidence_grade(rec), "C")

    def test_grade_a_for_multi_keyword_with_reviews(self):
        rec = {"probe_status": "confirmed_competitors",
               "query_stats": {"succeeded": 3, "requested": 3},
               "competitors": [{"review_evidence": "sufficient"},
                               {"review_evidence": "sufficient"}]}
        self.assertEqual(probe._derive_evidence_grade(rec), "A")

    def test_grade_b_for_confirmed_but_incomplete_reviews(self):
        rec = {"probe_status": "confirmed_competitors",
               "query_stats": {"succeeded": 3, "requested": 3},
               "competitors": [{"review_evidence": "insufficient"}]}
        self.assertEqual(probe._derive_evidence_grade(rec), "B")

    def test_audit_context_exposes_probe_status_and_error(self):
        probe_record = {
            "name": "标杆", "blank_signal": False,
            "probe_status": "request_error",
            "evidence_grade": "D",
            "query_stats": {"requested": 2, "succeeded": 0, "failed": 2, "non_empty": 0},
            "search_errors": [{"keyword": "kw1", "error": "boom"}],
            "competitors": [],
        }
        ctx = audit._build_audit_context(_bm(["kw1"]), probe_record)
        self.assertEqual(ctx["probe_status"], "request_error")
        self.assertEqual(ctx["is_blank"], "否（证据不足）")
        self.assertIn("search_errors", ctx)


if __name__ == "__main__":
    unittest.main()