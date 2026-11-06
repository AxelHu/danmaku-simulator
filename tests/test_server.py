"""Local-only preview regressions; every server binds an ephemeral loopback port."""
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from danmaku_sim.server import PreviewHandler


class PreviewServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / "runs"
        self.run = self.runs / "sample"
        self.web = self.root / "web"
        self.run.mkdir(parents=True)
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>test</title>")
        (self.run / "comments.json").write_text(json.dumps({
            "video": {"id": "test-video", "title": "Test", "duration": 10,
                      "path": "/private/source.mp4", "media_path": "/private/source.mp4"},
            "comments": [], "diagnostics": {"status": "completed"},
        }))
        self.video = self.root / "sample.mp4"
        self.video.write_bytes(b"0123456789")
        (self.run / "media.json").write_text(json.dumps({"path": str(self.video), "sha256": "test-video"}))
        (self.run / "config.json").write_text('{"private": "unit-test-only"}')
        handler = partial(PreviewHandler, runs_dir=self.runs, web_dir=self.web)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_public_response_hides_local_paths(self):
        status, _, body = self.request("/api/runs/sample")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertNotIn("path", document["video"])
        self.assertNotIn("media_path", document["video"])
        self.assertEqual(document["video"]["url"], "/api/runs/sample/video")
        self.assertNotIn("/private/", body.decode())
        self.assertTrue(document["synthetic"])

    def test_json_download_also_hides_local_media_paths(self):
        status, _, body = self.request("/api/runs/sample/export.json")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertNotIn("path", document["video"])
        self.assertNotIn("media_path", document["video"])
        self.assertNotIn("/private/", body.decode())
        self.assertEqual(document["comments"], [])

    def test_raw_private_files_and_traversal_have_no_route(self):
        for path in (
            "/api/runs/sample/config.json", "/api/runs/sample/media.json",
            "/api/runs/sample/usage.jsonl", "/api/runs/%2e%2e/config.json",
            "/../config.json",
        ):
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_malformed_run_is_skipped_and_returns_structured_error(self):
        bad = self.runs / "bad"
        bad.mkdir()
        (bad / "comments.json").write_text("[]")
        status, _, body = self.request("/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual([run["id"] for run in json.loads(body)["runs"]], ["sample"])
        status, _, body = self.request("/api/runs/bad")
        self.assertEqual(status, 422)
        self.assertIn("error", json.loads(body))

    def test_video_ranges_suffix_ranges_and_head(self):
        for header, expected, content_range in (
            ("bytes=2-5", b"2345", "bytes 2-5/10"),
            ("bytes=-3", b"789", "bytes 7-9/10"),
            ("bytes=9-", b"9", "bytes 9-9/10"),
        ):
            with self.subTest(header=header):
                status, headers, body = self.request(
                    "/api/runs/sample/video", headers={"Range": header})
                self.assertEqual(status, 206)
                self.assertEqual(body, expected)
                self.assertEqual(headers["Content-Range"], content_range)
                self.assertEqual(int(headers["Content-Length"]), len(expected))
        status, headers, body = self.request(
            "/api/runs/sample/video", method="HEAD", headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body, headers["Content-Length"]), (206, b"", "4"))
        status, headers, body = self.request(
            "/api/runs/sample/video", headers={"Range": "bytes=10-"})
        self.assertEqual((status, body), (416, b""))
        self.assertEqual(headers["Content-Range"], "bytes */10")

    def test_symlink_run_is_not_public(self):
        (self.runs / "alias").symlink_to(self.run, target_is_directory=True)
        status, _, _ = self.request("/api/runs/alias")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
