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
