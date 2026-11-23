"""Catalogue product output carries the full category path: Level 1 › Level 2 › Level 3."""
import os
import tempfile
import unittest
from unittest import mock

from tests._util import run_main
import inaproc

CATS = {"c3": {"id": "c3", "name": "Buku Tulis Lainnya", "level": 3,
               "parent": {"id": "c2", "name": "Buku Tulis", "level": 2,
                          "parent": {"id": "c1", "name": "Perlengkapan Alat Tulis", "level": 1, "parent": None}}}}


def search_item(n):
    return {"id": f"p{n}", "name": f"Kertas {n}", "username": "raka-kencana", "slug": f"kertas-{n}",
            "minPriceWithTax": 48000 + n, "maxPriceWithTax": 48000 + n, "sellerName": "RAKA KENCANA",
            "category": {"id": "c1", "name": "Perlengkapan Alat Tulis"}, "labels": ["PDN"], "unitSold": 5}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.limiter = mock.Mock()

    def gql(self, op, variables):
        self.calls.append(op)
        if op == "getProductCategory":
            return {"getProductCategory": CATS[variables["id"]]}
        if op == "getProductBySlug":
            return {"getProductBySlug": {"id": "x", "name": "Kertas", "username": variables["username"],
                                         "slug": variables["slug"], "category": {"id": "c3", "name": "Buku Tulis Lainnya"},
                                         "prices": {}, "variants": []}}
        if op == "searchProducts":
            return {"searchProducts": {"total": 2, "currentPage": 1, "lastPage": 1, "perPage": 20,
                                       "items": [search_item(1), search_item(2)]}}
        raise AssertionError(op)

    def gql_many(self, calls):
        return [self.gql(op, v) for op, v in calls]

    def close(self):
        pass


class CategoryLevelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.tmp.name})
        self.env.start()
        self.client = FakeClient()
        self.p = mock.patch.object(inaproc, "Client", return_value=self.client)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_chain(self):
        inaproc.CLIENT = self.client
        self.assertEqual(inaproc.category_chain("c3"), ["Perlengkapan Alat Tulis", "Buku Tulis", "Buku Tulis Lainnya"])
        inaproc.category_chain("c3")
        self.assertEqual(self.client.calls.count("getProductCategory"), 1)  # cached

    def test_search_shows_three_levels(self):
        code, out, err = run_main(["search", "kertas"])
        self.assertEqual(code, 0, err)
        for text in ("Kategori L1", "Kategori L2", "Kategori L3", "Buku Tulis Lainnya"):
            self.assertIn(text, out)

    def test_detail_shows_three_levels(self):
        code, out, err = run_main(["detail", "raka-kencana/kertas-1"])
        self.assertEqual(code, 0, err)
        self.assertIn("Kategori L1", out)
        self.assertIn("Perlengkapan Alat Tulis", out)
        self.assertIn("Buku Tulis Lainnya", out)

    def test_no_categories_flag_skips_lookups(self):
        code, out, err = run_main(["search", "kertas", "--no-categories"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("getProductBySlug", self.client.calls)


if __name__ == "__main__":
    unittest.main()
