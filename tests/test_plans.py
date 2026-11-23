import json
import os
import tempfile
import unittest

import tests._util  # noqa: F401
import inaproc
import inaproc_penyedia as ip

R = "<redacted>"


class FakeClient:
    def __init__(self, products, account="BUDI · PT CONTOH", signed_in=True, fail_ops=()):
        self.products = products
        self._account = account
        self._signed = signed_in
        self.fail_ops = set(fail_ops)
        self.sent = []

    def account(self):
        if not self._signed:
            raise ip.NotSignedIn("not signed in")
        return self._account

    def get_product(self, pid):
        return json.loads(json.dumps(self.products[pid]))

    def name_available(self, name):
        return all(p.get("name") != name for p in self.products.values())

    def gql(self, op, variables):
        if op in self.fail_ops:
            raise inaproc.InaprocError(f"{op}: BAD: nope (reqId r)")
        self.sent.append((op, variables))
        if op == "deleteProduct":
            return {"__typename": "StatusResponse", "data": {"status": "SUCCESS"}}
        return {"id": variables.get("id", "new1"), "name": "x", "jwtToken": "secret"}


class PlanTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("INAPROC_HOME")
        os.environ["INAPROC_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("INAPROC_HOME", None)
        else:
            os.environ["INAPROC_HOME"] = self.old
        self.tmp.cleanup()

    def plan(self, client, writes, kind="update"):
        return ip.make_plan(kind, client.account(), writes)


P1 = {"id": "p1", "name": "Kertas", "variants": [{"id": "v1", "price": 1000, "stock": 5}]}


class PlansTest(PlanTestBase):
    def test_state_hash_stable_and_sensitive(self):
        a = {"b": 1, "a": [1, 2]}
        self.assertEqual(ip.state_hash(a), ip.state_hash({"a": [1, 2], "b": 1}))
        self.assertNotEqual(ip.state_hash(a), ip.state_hash({"a": [1, 3], "b": 1}))

    def test_diff_lists_changed_paths(self):
        after = json.loads(json.dumps(P1))
        after["variants"][0]["price"] = 1200
        self.assertEqual(ip.diff(P1, after), [("variants[0].price", 1000, 1200)])

    def test_plan_written_mode_600(self):
        c = FakeClient({"p1": P1})
        path = self.plan(c, [{"op": "deleteProduct", "variables": {"id": "p1"}, "productId": "p1",
                              "beforeHash": ip.state_hash(P1), "summary": "delete Kertas"}], kind="delete")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        doc = json.loads(open(path).read())
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["account"], "BUDI · PT CONTOH")
        self.assertEqual(doc["kind"], "delete")
        self.assertTrue(str(path).startswith(os.path.join(self.tmp.name, "penyedia", "plan_")))

    def _delete_plan(self, c):
        return self.plan(c, [{"op": "deleteProduct", "variables": {"id": "p1"}, "productId": "p1",
                              "beforeHash": ip.state_hash(P1), "summary": "delete Kertas"}], kind="delete")

    def test_apply_sends_and_logs(self):
        c = FakeClient({"p1": P1})
        res = ip.apply_plan(c, self._delete_plan(c))
        self.assertEqual((res["done"], res["failed"], res["skipped"]), (1, 0, 0))
        self.assertEqual(c.sent, [("deleteProduct", {"id": "p1"})])

    def test_apply_requires_session(self):
        c = FakeClient({"p1": P1})
        path = self._delete_plan(c)
        c._signed = False
        with self.assertRaises(ip.NotSignedIn):
            ip.apply_plan(c, path)
        self.assertEqual(c.sent, [])

    def test_plan_account_mismatch(self):
        c = FakeClient({"p1": P1})
        path = self._delete_plan(c)
        c._account = "SOMEONE ELSE"
        with self.assertRaises(inaproc.InaprocError):
            ip.apply_plan(c, path)
        self.assertEqual(c.sent, [])

    def test_hash_changed_skips(self):
        c = FakeClient({"p1": P1})
        path = self._delete_plan(c)
        c.products["p1"] = dict(P1, name="Kertas (edited on the site)")
        res = ip.apply_plan(c, path)
        self.assertEqual((res["done"], res["skipped"]), (0, 1))
        self.assertEqual(c.sent, [])
        self.assertIn("changed since dry run", res["results"][0]["result"])

    def test_stop_on_first_failure(self):
        c = FakeClient({"p1": P1, "p2": dict(P1, id="p2")}, fail_ops={"updateProduct"})
        writes = [{"op": "updateProduct", "variables": {"id": "p1", "input": {}}, "productId": "p1",
                   "beforeHash": ip.state_hash(P1), "summary": "u1"},
                  {"op": "deleteProduct", "variables": {"id": "p2"}, "productId": "p2",
                   "beforeHash": ip.state_hash(dict(P1, id="p2")), "summary": "d2"}]
        res = ip.apply_plan(c, self.plan(c, writes))
        self.assertEqual((res["done"], res["failed"], res["notRun"]), (0, 1, 1))
        res = ip.apply_plan(c, self.plan(c, writes), continue_on_error=True)
        self.assertEqual((res["done"], res["failed"]), (1, 1))

    def test_log_appends_one_line_per_write(self):
        c = FakeClient({"p1": P1})
        ip.apply_plan(c, self._delete_plan(c))
        ip.apply_plan(c, self._delete_plan(c))
        log = os.path.join(self.tmp.name, "penyedia", "writes.log.jsonl")
        lines = open(log).read().splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["op"], "deleteProduct")
        self.assertEqual(entry["productId"], "p1")
        self.assertEqual(entry["result"], "done")
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)

    def test_log_redacts_response_tokens(self):
        c = FakeClient({})
        path = self.plan(c, [{"op": "createProduct", "variables": {"input": {"name": "N"}}, "summary": "create"}],
                         kind="create")
        ip.apply_plan(c, path)
        log = os.path.join(self.tmp.name, "penyedia", "writes.log.jsonl")
        self.assertEqual(json.loads(open(log).read().splitlines()[0])["response"]["jwtToken"], R)


if __name__ == "__main__":
    unittest.main()


SAMPLE = {
    "id": "src1", "masterProductId": None, "category": {"id": "cat1", "name": "Peralatan", "isActive": True},
    "categoryType": "GENERAL", "description": "Desc", "sellerId": "S1",
    "images": [{"id": "img1", "imageUrl": "https://asset.inaproc.id/upload/a.png", "isReadOnly": False},
               {"id": "img2", "imageUrl": "https://asset.inaproc.id/upload/master.png", "isReadOnly": True}],
    "isActive": True, "kbki": "3219200002", "name": "Kotak Akrilik",
    "pdn": {"countryCode": "IDN", "countryName": "Indonesia", "laborDescription": "x", "laborType": "ALL",
            "locationDescription": "y", "locationType": "ALL", "materialDescription": "z", "materialType": "ALL",
            "type": "LOCAL"},
    "preOrder": {"sla": 30}, "prices": {"minPurchase": 1, "productWholesalePrices": None},
    "tax": {"ppnPercentage": 12, "ppnBmId": None, "ppnBmTaxPaymentStatus": None, "ppnBmTaxPaymentFileToken": None},
    "shipping": {"dimensionUnit": "CM", "hasSellerShipping": False, "height": 10, "length": 10, "weight": 1000,
                 "weightUnit": "GR", "width": 10},
    "productInformations": {
        "additionalInformations": [{"id": "ai1", "name": "Informasi Lainnya", "value": "v1",
                                    "customFormFieldId": "f1", "isReadOnly": False}],
        "documents": [],
        "mainInformations": [{"id": "mi1", "name": "Spesifikasi Produk", "value": "spec", "customFormFieldId": "f2",
                              "isReadOnly": False},
                             {"id": "mi2", "name": "Locked", "value": "ro", "customFormFieldId": "f3",
                              "isReadOnly": True}]},
    "productOptions": None, "productAddOns": None, "status": "ACTIVE", "slug": "kotak-akrilik",
    "stockUnit": {"value": None, "secondaryUnit": None, "primaryUnit": "PAKET"}, "type": "PHYSICAL",
    "username": "toko", "variants": [{"id": "var1", "imageUrl": "", "isActive": True, "optionValues": None,
                                      "options": None, "price": 540541, "sku": "", "stock": 1000}],
    "videoSource": None, "videoUrl": None, "shippingOption": "STANDARD", "tkdn": None, "sni": None, "brand": None,
    "additionalFee": None, "hasDecimalQuantity": False,
}


def _all_keys(o, out=None):
    out = set() if out is None else out
    if isinstance(o, dict):
        for k, v in o.items():
            out.add(k)
            _all_keys(v, out)
    elif isinstance(o, list):
        for v in o:
            _all_keys(v, out)
    return out


class ConvertTest(unittest.TestCase):
    def test_create_from_clone_sets_new_name(self):
        inp = ip.clone_input(SAMPLE, name="TEST HAPUS 23-11", price=15000, stock=3)
        self.assertEqual(inp["name"], "TEST HAPUS 23-11")
        self.assertEqual(inp["category"], {"id": "cat1"})
        rest = {k: v for k, v in inp.items() if k != "category"}  # category.id picks the category
        self.assertNotIn("id", _all_keys(rest))  # a brand-new product: no ids copied from the source
        self.assertEqual(inp["images"], [{"imageUrl": "https://asset.inaproc.id/upload/a.png"}])
        self.assertEqual(inp["variants"], [{"price": 15000, "sku": "", "stock": 3, "options": None,
                                            "optionValues": None, "isActive": True, "imageUrl": ""}])
        self.assertEqual(inp["prices"], {"minPurchase": 1, "productWholesalePrices": []})
        self.assertEqual(inp["productInformations"]["mainInformations"],
                         [{"customFormFieldId": "f2", "name": "Spesifikasi Produk", "value": "spec"}])
        self.assertEqual(inp["pdn"], {"type": "LOCAL", "laborType": "ALL", "locationType": "ALL",
                                      "materialType": "ALL", "countryCode": "IDN"})
        self.assertEqual(inp["shipping"], {"width": 10, "weight": 1000, "length": 10, "height": 10,
                                           "hasSellerShipping": False, "dimensionUnit": "CM", "weightUnit": "GR"})
        self.assertEqual(inp["shippingOption"], "STANDARD")
        self.assertEqual(inp["logisticConfigurations"], [])
        self.assertEqual(inp["stockUnit"], {"primaryUnit": "PAKET", "value": None, "secondaryUnit": None})
        self.assertEqual(inp["preOrder"], {"sla": 30})
        self.assertEqual(inp["tax"], {"ppnPercentage": 12})
        for absent in ("tkdn", "sni", "brand", "kfa", "book", "construction", "videoToken"):
            self.assertNotIn(absent, inp)

    def test_shipping_units_converted_to_cm_gr(self):
        p = json.loads(json.dumps(SAMPLE))
        p["shipping"].update(weight=1.5, weightUnit="KG", length=0.2, dimensionUnit="M")
        s = ip.clone_input(p, name="N" * 10)["shipping"]
        self.assertEqual((s["weight"], s["length"], s["weightUnit"], s["dimensionUnit"]), (1500, 20, "GR", "CM"))

    def test_update_diff_lists_changed_fields_only(self):
        base = ip.update_input(SAMPLE, {})
        changed = ip.update_input(SAMPLE, {"price": 600000, "stock": 5})
        self.assertEqual(ip.diff(base, changed), [("variants[0].price", 540541, 600000),
                                                  ("variants[0].stock", 1000, 5)])
        self.assertEqual(base["variants"][0]["id"], "var1")
        self.assertEqual(base["images"], [{"id": "img1", "imageUrl": "https://asset.inaproc.id/upload/a.png"}])

    def test_update_name_and_set_path(self):
        u = ip.update_input(SAMPLE, {"name": "Kotak Akrilik Baru", "set": {"description": "D2"}})
        self.assertEqual((u["name"], u["description"]), ("Kotak Akrilik Baru", "D2"))

    def test_price_on_multi_variant_needs_variant(self):
        p = json.loads(json.dumps(SAMPLE))
        p["variants"].append(dict(p["variants"][0], id="var2", sku="B"))
        with self.assertRaises(inaproc.InaprocError):
            ip.update_input(p, {"price": 1})
        u = ip.update_input(p, {"price": 1, "variant": "var2"})
        self.assertEqual([v["price"] for v in u["variants"]], [540541, 1])


class WriteBuildersTest(PlanTestBase):
    def test_delete_plan_lists_name_and_link(self):
        c = FakeClient({"src1": SAMPLE})
        writes = ip.delete_writes(c, ["src1"])
        self.assertEqual(writes[0]["op"], "deleteProduct")
        self.assertEqual(writes[0]["variables"], {"id": "src1"})
        self.assertEqual(writes[0]["beforeHash"], ip.state_hash(SAMPLE))
        self.assertIn("Kotak Akrilik", writes[0]["summary"])
        self.assertIn("https://katalog.inaproc.id/toko/kotak-akrilik", writes[0]["summary"])

    def test_bulk_skips_unchanged_and_blocks_invalid(self):
        c = FakeClient({"src1": SAMPLE})
        writes, skipped, invalid = ip.bulk_writes(c, [
            {"id": "src1", "price": "540541", "stock": "1000"},   # unchanged
            {"id": "src1", "price": "abc"},                        # invalid
            {"id": "", "price": "5"}])                              # invalid
        self.assertEqual((len(writes), len(skipped), len(invalid)), (0, 1, 2))
        writes, skipped, invalid = ip.bulk_writes(c, [{"id": "src1", "price": "600.000", "stock": ""}])
        self.assertEqual((len(writes), invalid), (1, []))
        self.assertEqual(writes[0]["variables"]["input"]["variants"][0]["price"], 600000)
        self.assertEqual(writes[0]["variables"]["input"]["variants"][0]["stock"], 1000)


class CliFlowTest(PlanTestBase):
    def test_delete_dry_run_then_apply(self):
        from tests._util import run_main
        from unittest import mock
        c = FakeClient({"src1": SAMPLE})
        with mock.patch.object(ip, "_client", return_value=(mock.MagicMock(), c)):
            code, out, err = run_main(["penyedia", "product", "delete", "src1"])
            self.assertEqual(code, 0, err)
            self.assertEqual(c.sent, [])
            self.assertIn("Nothing was sent", out)
            plan = [l for l in out.splitlines() if "plan_" in l][0].split("penyedia apply ")[-1].strip().strip("`")
            code, out, err = run_main(["penyedia", "apply", plan])
        self.assertEqual(code, 0, err)
        self.assertEqual(c.sent, [("deleteProduct", {"id": "src1"})])
        self.assertIn("1 done", out)

    def test_create_clone_dry_run_uses_draft_by_default(self):
        from tests._util import run_main
        from unittest import mock
        c = FakeClient({"src1": SAMPLE})
        with mock.patch.object(ip, "_client", return_value=(mock.MagicMock(), c)):
            code, out, err = run_main(["penyedia", "product", "create", "--from", "src1", "--name", "TEST HAPUS 23-11"])
        self.assertEqual(code, 0, err)
        plan = [l for l in out.splitlines() if "plan_" in l][0].split("penyedia apply ")[-1].strip().strip("`")
        doc = json.loads(open(plan).read())
        self.assertEqual(doc["writes"][0]["op"], "createProductDraft")
        self.assertEqual(doc["writes"][0]["variables"]["input"]["name"], "TEST HAPUS 23-11")
