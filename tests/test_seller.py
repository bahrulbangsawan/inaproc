import json
import unittest
from unittest import mock

from tests._util import run_main
import inaproc_penyedia as ip
import inaproc


class FakeTab:
    """A penyedia tab whose page_fetch answers GraphQL calls from a dict {operationName: data or (status, text)}."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.closed = False

    def page_fetch(self, url, method="POST", headers=None, body=None, timeout=60):
        req = json.loads(body)
        self.calls.append(req)
        a = self.answers[req["operationName"]]
        if callable(a):
            a = a(req["variables"])
        if isinstance(a, tuple):
            return {"status": a[0], "headers": {}, "text": a[1]}
        return {"status": 200, "headers": {}, "text": json.dumps({"data": a})}


ITEM = {"id": "p1", "name": "Kertas HVS A4", "slug": "kertas-hvs-a4", "username": "tokoku", "status": "ACTIVE",
        "stockAccumulation": 40, "prices": {"minPrice": 50000, "maxPrice": 52000},
        "variants": [{"id": "v1"}, {"id": "v2"}]}


def fast_client(tab):
    return ip.SellerClient(tab, limiter=mock.MagicMock())


class SellerClientTest(unittest.TestCase):
    def test_refuses_unlisted_op(self):
        c = fast_client(FakeTab({}))
        with self.assertRaises(inaproc.InaprocError):
            c.gql("cartCheckoutWithoutNegotiation", {})

    def test_allowed_ops_are_exactly_the_product_ones(self):
        self.assertEqual(set(ip.ALLOWED_OPS), {
            "getListProduct", "getProductById", "checkProductNameAvailability", "getAllProductCategoryListSeller",
            "requestUploadSignedUrl", "createProduct", "createProductDraft", "updateProduct", "updateProductDraft",
            "deleteProduct", "updateProductActiveStatus", "getAllProductCategoryListSearch", "kbliByInstutionId"})
        for name, text in ip.ALLOWED_OPS.items():
            if name not in ("createProduct", "createProductDraft", "updateProduct", "updateProductDraft",
                            "deleteProduct", "updateProductActiveStatus"):
                self.assertTrue(text.lstrip().startswith("query"), name)

    def test_generic_error_is_failure(self):
        tab = FakeTab({"deleteProduct": {"deleteProduct": {"__typename": "GenericError", "code": "FORBIDDEN",
                                                           "message": "not yours", "reqId": "r1"}}})
        with self.assertRaises(inaproc.InaprocError) as cm:
            fast_client(tab).gql("deleteProduct", {"id": "x"})
        self.assertIn("not yours", str(cm.exception))
        self.assertIn("FORBIDDEN", str(cm.exception))

    def test_http_401_means_not_signed_in(self):
        tab = FakeTab({"getListProduct": (401, "unauthorized")})
        with self.assertRaises(ip.NotSignedIn):
            fast_client(tab).list_products()

    def test_graphql_errors_are_failures(self):
        tab = FakeTab({"getListProduct": (200, json.dumps({"errors": [{"message": "Variable bad"}]}))})
        with self.assertRaises(inaproc.InaprocError) as cm:
            fast_client(tab).list_products()
        self.assertIn("Variable bad", str(cm.exception))

    def test_list_products_rows(self):
        tab = FakeTab({"getListProduct": {"listProduct": {"items": [ITEM], "total": 1, "currentPage": 1,
                                                          "lastPage": 1, "perPage": 20}}})
        res = fast_client(tab).list_products(page=1, per_page=20)
        self.assertEqual(tab.calls[0]["variables"], {"page": 1, "perPage": 20, "filter": {}})
        self.assertEqual(ip.product_rows(res["items"]), [{
            "id": "p1", "name": "Kertas HVS A4", "variants": 2, "minPrice": 50000, "maxPrice": 52000,
            "stock": 40, "status": "ACTIVE", "link": "https://katalog.inaproc.id/tokoku/kertas-hvs-a4"}])


TREE = {"allMinifiedProductCategory": {"__typename": "AllMinifiedProductCategories", "items": [
    {"id": "L1", "level": 1, "name": "Perlengkapan Alat Tulis", "productType": "PHYSICAL", "parentId": None,
     "children": [{"id": "L2", "level": 2, "name": "Peralatan Surat", "productType": "PHYSICAL", "parentId": "L1",
                   "children": [{"id": "C1", "level": 3, "name": "Peralatan Surat Lainnya", "productType": "PHYSICAL"},
                                {"id": "C2", "level": 3, "name": "Amplop", "productType": "PHYSICAL"}]}]},
    {"id": "T1", "level": 1, "name": "Alat Transportasi", "productType": "PHYSICAL", "parentId": None,
     "children": [{"id": "T2", "level": 2, "name": "Alat Uji Kendaraan", "productType": "PHYSICAL", "parentId": "T1",
                   "children": [{"id": "C3", "level": 3, "name": "Alat Uji Kincup Roda", "productType": "PHYSICAL"},
                                {"id": "C4", "level": 3, "name": "Tanpa KBLI", "productType": "PHYSICAL"}]}]}]}}
KBLI = {"C1": ["18111", "17022"], "C2": [], "C3": ["28299"], "C4": None}
COMPANY = {"getInstitutionByID": {"__typename": "Institution", "company": {"skalaUsaha": "UMK", "oss": {
    "skalaUsaha": "MIKRO", "project": [{"kbli": [{"kbli": "18111", "name": "Industri Pencetakan Umum", "id": "k1",
                                                  "versi": "2020"}]},
                                       {"kbli": [{"kbli": "41019", "name": "Konstruksi Gedung Lainnya", "id": "k2",
                                                  "versi": "2020"}]}]}}}}


class BatchTab(FakeTab):
    def page_fetch(self, url, method="POST", headers=None, body=None, timeout=60):
        req = json.loads(body)
        if req.get("operationName") == "CategoryKbliBatch":
            self.calls.append(req)
            import re
            data = {}
            for alias, cid in re.findall(r'(c\d+):getProductCategory\(id:"([^"]+)"\)', req["query"]):
                data[alias] = {"id": cid, "kbli": KBLI.get(cid), "kbliCodes": None, "isActive": True}
            return {"status": 200, "headers": {}, "text": json.dumps({"data": data})}
        return super().page_fetch(url, method, headers, body, timeout)


class CategoryTest(unittest.TestCase):
    def test_eligibility_rule_matches_the_site(self):
        seller = {"18111", "41019"}
        self.assertTrue(ip.kbli_eligible(["18111", "17022"], seller))
        self.assertFalse(ip.kbli_eligible(["28299"], seller))
        self.assertFalse(ip.kbli_eligible([], seller))
        self.assertTrue(ip.kbli_eligible(None, seller))   # no KBLI list on the category: any KBLI will do
        self.assertFalse(ip.kbli_eligible(None, set()))

    def test_leaf_categories_with_paths(self):
        leaves = ip.leaf_categories(TREE["allMinifiedProductCategory"]["items"])
        self.assertEqual(leaves[0], {"id": "C1", "path": "Perlengkapan Alat Tulis > Peralatan Surat > Peralatan Surat Lainnya",
                                     "levels": ["Perlengkapan Alat Tulis", "Peralatan Surat", "Peralatan Surat Lainnya"],
                                     "productType": "PHYSICAL"})
        self.assertEqual(len(leaves), 4)

    def test_batch_lookup_is_read_only_and_chunked(self):
        tab = BatchTab({})
        c = fast_client(tab)
        ids = [f"X{i}" for i in range(120)]
        out = c.category_kbli(ids)
        self.assertEqual(len(tab.calls), 3)
        for call in tab.calls:
            self.assertTrue(call["query"].startswith("query CategoryKbliBatch"))
            self.assertNotIn("mutation", call["query"])
        self.assertEqual(set(out), set(ids))

    def test_company_kbli_deduplicated(self):
        tab = FakeTab({"kbliByInstutionId": COMPANY})
        self.assertEqual(fast_client(tab).company_kbli("S1"),
                         [{"code": "18111", "name": "Industri Pencetakan Umum", "version": "2020"},
                          {"code": "41019", "name": "Konstruksi Gedung Lainnya", "version": "2020"}])

    def test_categories_command_lists_eligible_and_caches(self):
        import os, tempfile
        tab = BatchTab({"getAllProductCategoryListSearch": TREE, "kbliByInstutionId": COMPANY})
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"INAPROC_HOME": d}), \
                mock.patch.object(ip, "open_penyedia", return_value=(mock.MagicMock(), tab)), \
                mock.patch.object(ip, "session_info", return_value={"signedIn": True}):
            code, out, err = run_main(["penyedia", "categories", "--seller-id", "S1", "--min-interval", "0.5"])
            self.assertEqual(code, 0, err)
            self.assertIn("Peralatan Surat Lainnya", out)
            self.assertIn("Tanpa KBLI", out)
            self.assertNotIn("Alat Uji Kincup Roda", out)
            self.assertNotIn("Amplop", out)
            self.assertIn("2 of 4", out)
            n = len(tab.calls)
            code, out, err = run_main(["penyedia", "categories", "--seller-id", "S1", "--all", "--search", "kincup"])
            self.assertEqual(code, 0, err)
            self.assertIn("Alat Uji Kincup Roda", out)
            self.assertEqual(len(tab.calls), n + 1)  # only the company KBLI again; categories came from the cache


class SellerIdCacheTest(unittest.TestCase):
    def test_seller_id_is_remembered(self):
        import os, tempfile
        tab = FakeTab({"getListProduct": {"listProduct": {"items": [ITEM]}},
                       "getProductById": {"getProduct": {"id": "p1", "sellerId": "S9"}}})
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"INAPROC_HOME": d}):
            self.assertEqual(fast_client(tab).seller_id(), "S9")
            n = len(tab.calls)
            self.assertEqual(fast_client(tab).seller_id(), "S9")
            self.assertEqual(len(tab.calls), n)


class CategoryExportTest(unittest.TestCase):
    def _run(self, extra, d):
        import os
        tab = BatchTab({"getAllProductCategoryListSearch": TREE, "kbliByInstutionId": COMPANY})
        with mock.patch.dict(os.environ, {"INAPROC_HOME": d}), \
                mock.patch.object(ip, "open_penyedia", return_value=(mock.MagicMock(), tab)), \
                mock.patch.object(ip, "session_info", return_value={"signedIn": True}):
            return run_main(["penyedia", "categories", "--seller-id", "S1"] + extra)

    def test_rows_have_three_levels_and_json_has_a_tree(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self._run(["--json"], d)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        row = doc["items"][0]
        self.assertEqual((row["level1"], row["level2"], row["level3"]),
                         ("Perlengkapan Alat Tulis", "Peralatan Surat", "Peralatan Surat Lainnya"))
        self.assertEqual(doc["tree"][0]["name"], "Perlengkapan Alat Tulis")
        self.assertEqual(doc["tree"][0]["children"][0]["name"], "Peralatan Surat")
        self.assertEqual(doc["tree"][0]["children"][0]["children"][0]["name"], "Peralatan Surat Lainnya")
        self.assertEqual(doc["tree"][0]["children"][0]["children"][0]["matchingKbli"], "18111")

    def test_markdown_groups_by_level(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self._run([], d)
        self.assertEqual(code, 0, err)
        self.assertIn("| Level 1 | Level 2 | Level 3 |", out)

    def test_saves_every_format(self):
        import os, tempfile, zipfile
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self._run(["--save", "json,csv,xlsx,yaml", "--out-dir", d], d)
            self.assertEqual(code, 0, err)
            files = sorted(os.listdir(os.path.join(d, "output")))
            self.assertEqual({f.rsplit(".", 1)[1] for f in files}, {"csv", "json", "xlsx", "yaml"})
            cat_csv = [f for f in files if f.endswith("__categories.csv")][0]
            csv_text = open(os.path.join(d, "output", cat_csv), encoding="utf-8-sig").read()
            self.assertIn("level1,level2,level3", csv_text.splitlines()[0].replace(" ", ""))
            self.assertIn("Peralatan Surat Lainnya", csv_text)
            x = [f for f in files if f.endswith(".xlsx")][0]
            with zipfile.ZipFile(os.path.join(d, "output", x)) as z:
                self.assertTrue(any("sheet" in n for n in z.namelist()))


class ExpiredTokenTest(unittest.TestCase):
    def test_expired_token_reloads_the_tab_and_retries_once(self):
        n = {"i": 0}

        def answer(v):
            n["i"] += 1
            if n["i"] == 1:
                return (200, json.dumps({"errors": [{"message": "ERR_EXPIRED_TOKEN"}]}))
            return {"listProduct": {"items": [], "total": 0}}
        tab = FakeTab({"getListProduct": answer})
        tab.navigate = mock.Mock()
        tab.wait_load = mock.Mock(return_value=True)
        res = fast_client(tab).list_products()
        self.assertEqual(res["total"], 0)
        tab.navigate.assert_called_once_with("https://penyedia.inaproc.id/products")

    def test_still_expired_after_reload_means_sign_in_again(self):
        tab = FakeTab({"getListProduct": (200, json.dumps({"errors": [{"message": "ERR_EXPIRED_TOKEN"}]}))})
        tab.navigate = mock.Mock()
        tab.wait_load = mock.Mock(return_value=True)
        with self.assertRaises(ip.NotSignedIn):
            fast_client(tab).list_products()


class ProductsCommandTest(unittest.TestCase):
    def test_products_command_prints_table(self):
        tab = FakeTab({"getListProduct": {"listProduct": {"items": [ITEM], "total": 1, "currentPage": 1,
                                                          "lastPage": 1, "perPage": 20}}})
        with mock.patch.object(ip, "open_penyedia", return_value=(mock.MagicMock(), tab)), \
                mock.patch.object(ip, "session_info", return_value={"signedIn": True}):
            code, out, err = run_main(["penyedia", "products", "--min-interval", "0.5"])
        self.assertEqual(code, 0, err)
        self.assertIn("Kertas HVS A4", out)
        self.assertIn("1 of 1", out)


if __name__ == "__main__":
    unittest.main()
