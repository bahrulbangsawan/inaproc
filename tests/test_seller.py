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
            "deleteProduct", "updateProductActiveStatus"})

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
