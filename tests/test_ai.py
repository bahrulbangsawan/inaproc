import json
import os
import tempfile
import unittest
from unittest import mock

import tests._util  # noqa: F401
import inaproc_ai as ai


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, headers, body=None, timeout=120):
        self.requests.append({"method": method, "url": url, "headers": dict(headers),
                              "body": json.loads(json.dumps(body)) if body is not None else None})
        return self.replies.pop(0)


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, name, args):
        self.calls.append((name, args))
        return "PPN rates: 12%", False


class AnthropicLoopTest(unittest.TestCase):
    def test_tool_loop_echoes_content_and_returns_text(self):
        first = {"stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Saya cek dulu."},
            {"type": "tool_use", "id": "toolu_1", "name": "inaproc", "input": {"args": ["ppn"]}}]}
        second = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "PPN di katalog **12%**."}]}
        http = FakeHTTP([(200, first), (200, second)])
        runner = FakeRunner()
        agent = ai.Agent("anthropic", "sk-ant-x", "claude-opus-5-5", runner=runner, http=http, system="S")
        out = agent.send("berapa ppn?")
        self.assertEqual(out, "PPN di katalog **12%**.")
        self.assertEqual(runner.calls, [("inaproc", {"args": ["ppn"]})])
        r0, r1 = http.requests
        self.assertEqual(r0["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(r0["headers"]["x-api-key"], "sk-ant-x")
        self.assertEqual(r0["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(r0["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")
        self.assertEqual(r0["body"]["fallbacks"], "default")
        self.assertEqual(r0["body"]["model"], "claude-opus-5-5")
        self.assertNotIn("temperature", r0["body"])
        msgs = r1["body"]["messages"]
        self.assertEqual(msgs[1], {"role": "assistant", "content": first["content"]})  # unchanged, thinking kept
        self.assertEqual(msgs[2]["role"], "user")
        self.assertEqual(msgs[2]["content"][0]["type"], "tool_result")
        self.assertEqual(msgs[2]["content"][0]["tool_use_id"], "toolu_1")

    def test_no_fallbacks_for_other_models(self):
        http = FakeHTTP([(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]})])
        ai.Agent("anthropic", "k", "claude-haiku-5-5", runner=FakeRunner(), http=http, system="S").send("hi")
        self.assertNotIn("fallbacks", http.requests[0]["body"])
        self.assertNotIn("anthropic-beta", http.requests[0]["headers"])

    def test_refusal_is_reported(self):
        http = FakeHTTP([(200, {"stop_reason": "refusal", "content": [], "stop_details": {"category": "cyber"}})])
        out = ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S").send("x")
        self.assertIn("menolak", out)

    def test_bad_key_message(self):
        http = FakeHTTP([(401, {"error": {"message": "invalid x-api-key"}})])
        with self.assertRaises(ai.AIError) as cm:
            ai.Agent("anthropic", "k", "claude-opus-5-5", runner=FakeRunner(), http=http, system="S").send("x")
        self.assertIn("inaproc setup", str(cm.exception))


class OpenAILoopTest(unittest.TestCase):
    def test_tool_calls_loop(self):
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "inaproc", "arguments": json.dumps({"args": ["ppn"]})}}]},
            "finish_reason": "tool_calls"}]}
        second = {"choices": [{"message": {"role": "assistant", "content": "PPN 12%."}, "finish_reason": "stop"}]}
        http = FakeHTTP([(200, first), (200, second)])
        runner = FakeRunner()
        agent = ai.Agent("deepseek", "sk-ds", "deepseek-chat", runner=runner, http=http, system="S")
        self.assertEqual(agent.send("ppn?"), "PPN 12%.")
        r0, r1 = http.requests
        self.assertEqual(r0["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(r0["headers"]["Authorization"], "Bearer sk-ds")
        self.assertEqual(r0["body"]["messages"][0], {"role": "system", "content": "S"})
        self.assertEqual(r0["body"]["tools"][0]["type"], "function")
        tool_msg = r1["body"]["messages"][-1]
        self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "call_1"))

    def test_bad_tool_arguments_become_an_error_result(self):
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "inaproc", "arguments": "{not json"}}]}}]}
        second = {"choices": [{"message": {"role": "assistant", "content": "maaf"}}]}
        http = FakeHTTP([(200, first), (200, second)])
        runner = FakeRunner()
        ai.Agent("openai", "k", "m", runner=runner, http=http, system="S").send("x")
        self.assertEqual(runner.calls, [])
        self.assertIn("error", http.requests[1]["body"]["messages"][-1]["content"].lower())


class ModelsTest(unittest.TestCase):
    def test_list_models(self):
        http = FakeHTTP([(200, {"data": [{"id": "claude-opus-5-5"}, {"id": "claude-haiku-5-5"}]})])
        self.assertEqual(ai.list_models("anthropic", "k", http=http), ["claude-opus-5-5", "claude-haiku-5-5"])
        self.assertEqual(http.requests[0]["url"], "https://api.anthropic.com/v1/models")
        http = FakeHTTP([(200, {"data": [{"id": "deepseek-chat"}]})])
        self.assertEqual(ai.list_models("deepseek", "k", http=http), ["deepseek-chat"])
        self.assertEqual(http.requests[0]["url"], "https://api.deepseek.com/models")


class ToolRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INAPROC_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_blocked_commands_are_refused(self):
        r = ai.ToolRunner(executor=mock.Mock(), confirm=mock.Mock())
        for args in (["penyedia", "apply", "x.json"], ["update"], ["discover"], ["penyedia", "record"],
                     ["penyedia", "logout"], ["login"]):
            text, err = r.run("inaproc", {"args": args})
            self.assertTrue(err, args)
        r.executor.assert_not_called()

    def test_inaproc_tool_runs_the_cli(self):
        ex = mock.Mock(return_value=(0, "hasil", ""))
        r = ai.ToolRunner(executor=ex, confirm=mock.Mock())
        text, err = r.run("inaproc", {"args": ["search", "kertas"]})
        self.assertFalse(err)
        self.assertIn("hasil", text)
        self.assertEqual(ex.call_args[0][0], ["search", "kertas"])

    def test_apply_plan_needs_the_user_in_the_terminal(self):
        pdir = os.path.join(self.tmp.name, "penyedia")
        os.makedirs(pdir)
        plan = os.path.join(pdir, "plan_1_delete.json")
        json.dump({"version": 1, "kind": "delete", "account": "A", "writes": [
            {"op": "deleteProduct", "summary": "DELETE Kotak"}]}, open(plan, "w"))
        ex = mock.Mock(return_value=(0, "1 done", ""))
        r = ai.ToolRunner(executor=ex, confirm=mock.Mock(return_value=False))
        text, err = r.run("apply_plan", {"plan": plan})
        self.assertIn("nothing was sent", text.lower())
        ex.assert_not_called()
        r.confirm.return_value = True
        text, err = r.run("apply_plan", {"plan": plan})
        sent = ex.call_args[0][0]
        self.assertEqual(sent[:2], ["penyedia", "apply"])
        self.assertEqual(os.path.realpath(sent[2]), os.path.realpath(plan))

    def test_apply_plan_only_from_the_plans_folder(self):
        r = ai.ToolRunner(executor=mock.Mock(), confirm=mock.Mock(return_value=True))
        text, err = r.run("apply_plan", {"plan": "/etc/passwd"})
        self.assertTrue(err)
        r.executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
