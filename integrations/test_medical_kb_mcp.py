import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from medical_kb_mcp import KB, Server, ToolError, validate_base


HIT = {"id": "fresh-5", "file": "示例科目/教材.md", "heading": "示例标题", "text": "合成教材证据，不是真实书籍正文。",
       "pages": ["5"], "source_sha256": "a" * 64, "content_hash": "b" * 64,
       "original_source": "示例科目/教材.pdf", "original_source_sha256": "c" * 64,
       "source_kind": "教材正文", "provider_secret": "must-not-be-forwarded"}


class Backend(BaseHTTPRequestHandler):
    requests = []
    response = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, body, dict(self.headers)))
        if body["query"] == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.1/no-network")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        payload = self.response if self.response is not None else {"results": [HIT]}
        self.wfile.write(json.dumps(payload).encode("utf-8"))


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        Backend.requests = []
        Backend.response = None
        self.kb = KB(self.base)

    def test_literal_loopback_only(self):
        for value in ["https://127.0.0.1:8765", "http://localhost:8765", "http://example.com:80",
                      "http://127.0.0.1:8765/api", "http://user:pass@127.0.0.1:8765",
                      "http://127.0.0.1:8765?q=x", "http://127.0.0.1:99999", "file:///tmp/x"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_base(value)
        self.assertEqual(validate_base("http://[::1]:8765/"), "http://[::1]:8765")

    def test_search_contract_and_citations(self):
        data = self.kb.call("medical_kb_search", {"query": "示例科目 示例主题", "subject": "示例科目", "limit": 2})
        path, body, headers = Backend.requests[0]
        self.assertEqual(path, "/api/search")
        self.assertEqual(body, {"query": "示例科目 示例主题", "k": 2, "mode": "search", "path_filter": "示例科目"})
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("provider_secret", data["results"][0])
        self.assertTrue(data["results"][0]["original_source_reference_present"])
        self.assertFalse(data["results"][0]["original_source_verified"])
        self.assertEqual(data["results"][0]["pages"], ["5"])
        self.assertFalse(data["llm_called_by_adapter"])

    def test_fetch_is_snapshot_without_network(self):
        hit = self.kb.call("medical_kb_search", {"query": "问诊"})["results"][0]
        result = self.kb.call("medical_kb_fetch", {"hit_id": hit["hit_id"]})
        self.assertEqual(result["result"], hit)
        self.assertTrue(result["snapshot"])
        self.assertEqual(len(Backend.requests), 1)

    def test_fetch_does_not_read_arbitrary_path(self):
        for args in [{"hit_id": "../../config.json"}, {"path": "C:/secret"}, {"hit_id": "f" * 64}]:
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.kb.call("medical_kb_fetch", args)
        self.assertEqual(Backend.requests, [])

    def test_missing_citation_not_invented(self):
        Backend.response = {"results": [{"text": "synthetic", "file": "chapter.md"}]}
        hit = self.kb.call("medical_kb_search", {"query": "x"})["results"][0]
        self.assertEqual(hit["citation_missing"], ["id", "pages", "source_sha256", "content_hash", "original_source", "original_source_sha256"])
        self.assertFalse(hit["original_source_verified"])
        self.assertNotIn("original_source", hit)

    def test_missing_original_reported_even_when_snippet_fields_complete(self):
        hit = dict(HIT)
        del hit['original_source']
        del hit['original_source_sha256']
        Backend.response = {'results': [hit]}
        result = self.kb.call('medical_kb_search', {'query': 'synthetic'})['results'][0]
        self.assertEqual(result['citation_missing'], ['original_source', 'original_source_sha256'])
        self.assertFalse(result['original_source_reference_present'])
        self.assertFalse(result['original_source_verified'])

    def test_outline_does_not_claim_textbook_is_formal_outline(self):
        data = self.kb.call("medical_kb_outline", {"subject": "示例科目"})
        self.assertEqual(data["matching_outline_hits"], [])
        self.assertFalse(data["complete_outline_verified"])
        self.assertEqual(Backend.requests[0][1]["mode"], "search")

    def test_outline_marks_only_actual_kind(self):
        Backend.response = {"results": [dict(HIT, source_kind="课程教学大纲")]}
        data = self.kb.call("medical_kb_outline", {"subject": "示例科目", "kind": "teaching"})
        self.assertEqual(data["matching_outline_hits"], [data["results"][0]["hit_id"]])

    def test_reject_modes_paths_and_bad_limits(self):
        cases = [{"query": "x", "mode": "ask"}, {"query": "x", "subject": "../foo"},
                 {"query": "x", "limit": True}, {"query": "x", "limit": 9}, {"query": ""},
                 {"query": "x", "url": "http://example.com"}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.kb.call("medical_kb_search", args)
        self.assertEqual(Backend.requests, [])

    def test_redirect_not_followed(self):
        with self.assertRaisesRegex(ToolError, "KB_UNAVAILABLE"):
            self.kb.call("medical_kb_search", {"query": "redirect"})
        self.assertEqual(len(Backend.requests), 1)

    def test_invalid_backend_schema(self):
        for data in [{"results": "oops"}, {"results": [False]}, []]:
            Backend.response = data
            with self.subTest(data=data), self.assertRaises(ToolError):
                self.kb.call("medical_kb_search", {"query": "x"})

    def test_cache_bounded(self):
        self.kb.cache_size = 1
        first = self.kb.call("medical_kb_search", {"query": "x"})["results"][0]["hit_id"]
        Backend.response = {"results": [dict(HIT, text="different evidence")]}
        self.kb.call("medical_kb_search", {"query": "y"})
        with self.assertRaises(ToolError):
            self.kb.call("medical_kb_fetch", {"hit_id": first})

    def test_protocol_requires_initialization(self):
        server = Server(self.kb)
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(result["error"]["code"], -32002)

    def test_real_stdio_roundtrip(self):
        proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("medical_kb_mcp.py")), "--kb-base", self.base],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
        def send(value, response=True):
            proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline()) if response else None
        try:
            init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "contract-test", "version": "1"}, "capabilities": {}}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"}, False)
            listing = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(listing["result"]["tools"]), 3)
            response = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "medical_kb_search", "arguments": {"query": "问诊"}}})
            hit_id = json.loads(response["result"]["content"][0]["text"])["results"][0]["hit_id"]
            fetched = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "medical_kb_fetch", "arguments": {"hit_id": hit_id}}})
            self.assertFalse(fetched["result"]["isError"])
            self.assertEqual(json.loads(fetched["result"]["content"][0]["text"])["result"]["file"], HIT["file"])
            error = send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "medical_kb_search", "arguments": {"query": "x", "mode": "ask"}}})
            self.assertTrue(error["result"]["isError"])
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(proc.stderr.read(), "")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
