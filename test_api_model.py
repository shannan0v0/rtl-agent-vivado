"""Exercise the public CLI and its HTTP requests without a paid provider."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RTL = "module TopModule(input wire a, output wire y); assign y=a; endmodule"
KEY = "fixture-key-for-http-test"


class ProviderCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.task = self.root / "input"
        self.task.mkdir()
        (self.task / "prompt.txt").write_text("Implement this interface: " + RTL.split(";")[0] + ";", encoding="utf-8")
        self.output = self.root / "output"
        self.requests = []
        self.responses = []
        requests, responses = self.requests, self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                status, data = responses.pop(0)
                self.send_response(status)
                self.send_header("X-Private-Header", KEY)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def invoke(self):
        url = f"http://127.0.0.1:{self.server.server_port}/v1"
        env = dict(os.environ, RTL_API_KEY=KEY, VIVADO_BIN=str(self.root / "missing-vivado"),
                   NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        return subprocess.run(
            [sys.executable, str(ROOT / "run_api_model.py"), str(self.task), str(self.output),
             "--base-url", url, "--model", "fixture-model"],
            env=env, capture_output=True, text=True, timeout=20,
        )

    def assert_no_key_in_files(self):
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), path)

    def test_first_call_and_repair_use_selected_provider(self):
        for content in [RTL.replace("TopModule", "Wrong"), RTL]:
            self.responses.append((200, {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}))
        run = self.invoke()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((self.output / "solution.v").read_text().strip(), RTL)
        self.assertEqual(json.loads(run.stdout)["compile_status"], "unavailable")
        self.assertEqual(len(self.requests), 2)
        for path, auth, body in self.requests:
            self.assertEqual((path, auth, body["model"]), ("/v1/chat/completions", "Bearer " + KEY, "fixture-model"))
            self.assertEqual(set(body), {"model", "messages"})
        self.assertIn("Wrong", str(self.requests[1][2]["messages"]))
        self.assert_no_key_in_files()

    def test_unauthorized_request_reports_failure_without_retry(self):
        self.responses.append((401, {"error": "rejected " + KEY}))
        run = self.invoke()
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["http_status"], 401)
        self.assertEqual(len(self.requests), 1)
        self.assert_no_key_in_files()


if __name__ == "__main__":
    unittest.main()
