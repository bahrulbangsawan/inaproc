"""Regression tests for the final review's Important findings (#1-#10)."""
import ast
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._util import run_main, SCRIPTS
import inaproc
import inaproc_penyedia as ip
import inaproc_safari as sf
from tests.test_seller import FakeTab, fast_client
from tests.test_plans import FakeClient, PlanTestBase, SAMPLE, P1
from tests.test_safari import FakeRunner, LIST


class Py38Test(unittest.TestCase):  # 1
    def test_no_dict_union_operator(self):
        tree = ast.parse(open(os.path.join(SCRIPTS, "inaproc_penyedia.py")).read())
        bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
               and (isinstance(n.left, (ast.Dict, ast.Call)) or isinstance(n.right, (ast.Dict, ast.Call)))]
        self.assertEqual(bad, [])


class GqlFailureTest(unittest.TestCase):  # 2, 3
    def test_http_500_json_is_failure(self):
        with self.assertRaises(inaproc.InaprocError):
            fast_client(FakeTab({"deleteProduct": (500, '{"message":"internal"}')})).gql("deleteProduct", {"id": "x"})

    def test_empty_or_null_write_result_is_failure(self):
        for data in ({"deleteProduct": {}}, {"deleteProduct": None}):
            with self.assertRaises(inaproc.InaprocError):
                fast_client(FakeTab({"deleteProduct": data})).gql("deleteProduct", {"id": "x"})

    def test_good_write_results_pass(self):
        ok = fast_client(FakeTab({"deleteProduct": {"deleteProduct": {"__typename": "StatusResponse",
                                                                      "data": {"status": "SUCCESS"}}}}))
        self.assertTrue(ok.gql("deleteProduct", {"id": "x"}))
        ok = fast_client(FakeTab({"updateProduct": {"updateProduct": {"id": "x", "name": "n"}}}))
        self.assertEqual(ok.gql("updateProduct", {"id": "x", "input": {}})["id"], "x")

    def test_writes_are_not_retried_on_gateway_errors(self):
        tab = FakeTab({"createProductDraft": (503, "busy")})
        with mock.patch("time.sleep"), self.assertRaises(inaproc.InaprocError):
            fast_client(tab).gql("createProductDraft", {"input": {}})
        self.assertEqual(len(tab.calls), 1)

    def test_reads_are_retried_on_gateway_errors(self):
        n = {"i": 0}

        def flaky(v):
            n["i"] += 1
            return (503, "busy") if n["i"] == 1 else {"getProduct": {"id": "p"}}
        tab = FakeTab({"getProductById": flaky})
        with mock.patch("time.sleep"):
            self.assertEqual(fast_client(tab).get_product("p")["id"], "p")


class ApplyRobustnessTest(PlanTestBase):  # 4, 8
    def _plan(self, c, writes=None, kind="delete"):
        writes = writes or [{"op": "deleteProduct", "variables": {"id": "p1"}, "productId": "p1",
                             "beforeHash": ip.state_hash(P1), "summary": "d"}]
        return ip.make_plan(kind, c.account(), writes)

    def test_unexpected_exception_is_logged_and_summarised(self):
        c = FakeClient({"p1": P1})
        c.gql = mock.Mock(side_effect=sf.SafariError("Safari did not answer in time"))
        res = ip.apply_plan(c, self._plan(c))
        self.assertEqual(res["failed"], 1)
        self.assertIn("unknown", res["results"][0]["result"])
        log = open(os.path.join(self.tmp.name, "penyedia", "writes.log.jsonl")).read().splitlines()
        self.assertEqual([json.loads(l)["result"] for l in log], ["sending", "failed (outcome unknown)"])

    def test_hand_edited_plan_is_refused(self):
        c = FakeClient({"p1": P1})
        path = self._plan(c)
        doc = json.load(open(path))
        doc["writes"][0]["variables"]["id"] = "p2"
        json.dump(doc, open(path, "w"))
        with self.assertRaises(inaproc.InaprocError) as cm:
            ip.apply_plan(c, path)
        self.assertIn("changed after the dry run", str(cm.exception))

    def test_update_or_delete_without_before_hash_is_refused(self):
        c = FakeClient({"p1": P1})
        with self.assertRaises(inaproc.InaprocError):  # refused when the plan is made
            self._plan(c, [{"op": "deleteProduct", "variables": {"id": "p1"}, "productId": "p1", "summary": "d"}])
        path = self._plan(c)  # and at apply, for a plan whose beforeHash was removed by hand
        doc = json.load(open(path))
        del doc["writes"][0]["beforeHash"]
        json.dump(doc, open(path, "w"))
        with self.assertRaises(inaproc.InaprocError):
            ip.apply_plan(c, path)
        self.assertEqual(c.sent, [])

    def test_plan_cannot_be_applied_twice(self):
        c = FakeClient({})
        path = ip.make_plan("create", c.account(), [{"op": "createProductDraft", "variables": {"input": {"name": "N"}},
                                                      "summary": "c"}])
        ip.apply_plan(c, path)
        with self.assertRaises(inaproc.InaprocError) as cm:
            ip.apply_plan(c, path)
        self.assertIn("already applied", str(cm.exception))
        self.assertEqual(len(c.sent), 1)

    def test_unknown_account_is_refused(self):
        c = FakeClient({"p1": P1}, account="unknown")
        with self.assertRaises(inaproc.InaprocError):
            self._plan(c)


class SafariErrorsTest(unittest.TestCase):  # 5
    def test_safari_error_prints_cleanly(self):
        with mock.patch.object(ip, "open_penyedia", side_effect=sf.SafariError(sf.SETTING_HELP)):
            code, out, err = run_main(["penyedia", "status", "--browser", "safari"])
        self.assertEqual(code, 1)
        self.assertIn("Allow JavaScript from Apple Events", err)
        self.assertNotIn("Traceback", err)


class SafariTabIdentityTest(unittest.TestCase):  # 6, 7
    def test_new_tab_uses_the_tab_it_opened(self):
        r = FakeRunner([("make new tab", "33\t2"), ("every tab", LIST)])
        tab = sf.Safari(runner=r).new_tab("https://penyedia.inaproc.id/products")
        self.assertEqual((tab.window_id, tab.index), (33, 2))

    def test_page_fetch_refuses_a_tab_on_another_origin(self):
        r = FakeRunner([("every tab", LIST), ("do JavaScript", json.dumps({"status": 200, "text": "{}"}))])
        tab = sf.Safari(runner=r).find_tab("https://penyedia.inaproc.id")
        tab.page_fetch("https://penyedia.inaproc.id/graphql", body="{}")
        self.assertIn("location.origin!==\"https://penyedia.inaproc.id\"", r.calls[-1][-3].replace(" ", ""))

    def test_hostname_must_match_exactly(self):
        self.assertTrue(ip.on_penyedia("https://penyedia.inaproc.id/products"))
        self.assertFalse(ip.on_penyedia("https://penyedia.inaproc.id.evil.example/"))
        s = sf.Safari(runner=FakeRunner([("every tab", "1\t1\thttps://penyedia.inaproc.id.evil.example/\n")]))
        self.assertIsNone(s.find_tab("https://penyedia.inaproc.id"))


class BulkPerVariantTest(PlanTestBase):  # 9
    def test_rows_for_the_same_product_become_one_write(self):
        p = json.loads(json.dumps(SAMPLE))
        p["variants"].append(dict(p["variants"][0], id="var2", sku="B", price=100, stock=1))
        c = FakeClient({"src1": p})
        writes, skipped, invalid = ip.bulk_writes(c, [{"id": "src1", "variant": "var1", "price": "1"},
                                                      {"id": "src1", "variant": "B", "stock": "9"}])
        self.assertEqual((len(writes), invalid), (1, []))
        v = writes[0]["variables"]["input"]["variants"]
        self.assertEqual([(x["price"], x["stock"]) for x in v], [(1, 1000), (100, 9)])


class DryRunPayloadTest(PlanTestBase):  # 10
    def test_create_dry_run_prints_the_full_payload(self):
        c = FakeClient({"src1": SAMPLE})
        with mock.patch.object(ip, "_client", return_value=(mock.MagicMock(), c)):
            code, out, err = run_main(["penyedia", "product", "create", "--from", "src1", "--name", "TEST HAPUS 1"])
        self.assertEqual(code, 0, err)
        self.assertIn('"productInformations"', out)
        self.assertIn('"Spesifikasi Produk"', out)

    def test_update_dry_run_points_to_the_full_payload(self):
        c = FakeClient({"src1": SAMPLE})
        with mock.patch.object(ip, "_client", return_value=(mock.MagicMock(), c)):
            code, out, err = run_main(["penyedia", "product", "update", "src1", "--price", "600000"])
        self.assertEqual(code, 0, err)
        self.assertIn("Full payload", out)


if __name__ == "__main__":
    unittest.main()
