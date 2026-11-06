"""Publishing is explicit; local files and HTTP consumers share the same bytes."""
from copy import deepcopy
from functools import partial
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

from danmaku_sim.distribution import (aligned_comments, canonical_url, load_catalog,
                                      make_track, match_tracks, publish_run, write_sidecar)
from danmaku_sim.server import PreviewHandler


def sample():
    return {"video": {"id": "a" * 64, "title": "Episode", "duration": 10,
                      "path": "/private/video.mp4", "url": "https://private.invalid/token"},
            "comments": [{"id": "a", "time": 1, "text": "这是什么", "persona": "casual",
                          "source_event": {"private": "unpublished evidence"}, "provider": "private-provider"},
                         {"id": "b", "time": 3, "text": "像一只小鸟", "persona": "detail",
                          "generation_method": "interaction", "reply_to": "a", "parent_id": "a",
                          "parent_time": 1, "depth": 1, "interaction_type": "reply"},
                         {"id": "c", "time": 9, "text": "来了"}],
            "candidates": [{"private": "do not distribute"}], "diagnostics": {"private": "run-only"}}


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "runs" / "example"
        self.run.mkdir(parents=True)
        self.library = self.root / "published"
        self.result = sample()
        self.save_run()
        (self.run / "config.json").write_text('{"private_key":"never-read"}')

    def save_run(self):
        (self.run / "comments.json").write_text(json.dumps(self.result, ensure_ascii=False))

    def test_publication_strips_run_details_and_has_verifiable_portable_files(self):
        output = publish_run(self.run, self.library, label="少量互动")
        catalog = load_catalog(self.library)
        self.assertEqual(len(catalog["items"]), 1)
        self.assertFalse((self.library / ".catalog.lock").exists())
        for extension, artifact in output["track"]["formats"].items():
            data = (self.library / artifact["href"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), artifact["sha256"])
            self.assertEqual(len(data), artifact["bytes"])
            self.assertNotIn(b"private", data)
            self.assertNotIn(str(self.root).encode(), data)
        track = json.loads((self.library / output["track"]["formats"]["json"]["href"]).read_text())
        self.assertEqual(track["format"], "danmaku-track/v1")
        self.assertEqual(track["comments"][1]["reply_to"], "a")
        self.assertNotIn("candidates", track)
        self.assertEqual(track["timeline"], {"offset": 0.0, "scale": 1.0})

    def test_repeat_publish_is_idempotent_and_new_revision_does_not_overwrite_old(self):
        first = publish_run(self.run, self.library)
        original_path = self.library / first["track"]["formats"]["json"]["href"]
        original = original_path.read_bytes()
        again = publish_run(self.run, self.library)
        self.assertTrue(again["reused_revision"])
        self.assertEqual(first["track"], again["track"])
        self.result["comments"][0]["text"] = "新版本"
        self.save_run()
        changed = publish_run(self.run, self.library)
        self.assertNotEqual(first["track"]["revision"], changed["track"]["revision"])
        self.assertEqual(original_path.read_bytes(), original)
        self.assertEqual(len(load_catalog(self.library)["items"][0]["tracks"]), 1)

    def test_variants_share_media_but_have_separate_track_ids(self):
        publish_run(self.run, self.library, track_id="quiet")
        publish_run(self.run, self.library, track_id="social")
        self.assertEqual([t["id"] for t in load_catalog(self.library)["items"][0]["tracks"]], ["quiet", "social"])

    def test_time_transform_is_baked_into_xml_and_clips_only_outside_target(self):
        output = publish_run(self.run, self.library, timeline={"offset": -1, "scale": 2, "target_duration": 10})
        path = self.library / output["track"]["formats"]["xml"]["href"]
        times = [float(row.attrib["p"].split(",")[0]) for row in ET.parse(path).getroot().findall("d")]
        self.assertEqual(times, [1, 5])
        self.assertEqual(output["track"]["alignment"]["dropped_outside_timeline"], 1)
        self.assertEqual(output["track"]["formats"]["json"]["comment_count"], 3)
        self.assertEqual(output["track"]["formats"]["xml"]["comment_count"], 2)
        track = make_track(self.result, timeline={"offset": 2})
        self.assertEqual(aligned_comments(track)[0][0]["time"], 3)

    def test_matching_requires_part_and_separately_checks_duration(self):
        publish_run(self.run, self.library, bindings=[{"site": "bilibili", "bvid": "BV17x411w7KC", "page": 2, "cid": "123"}])
        catalog = load_catalog(self.library)
        partial_match = match_tracks(catalog, bvid="BV17x411w7KC")
        self.assertEqual(len(partial_match), 1)
        self.assertFalse(partial_match[0]["match"]["eligible_for_selection"])
        self.assertEqual(match_tracks(catalog, bvid="BV17x411w7KC", page=1), [])
        self.assertEqual(match_tracks(catalog, bvid="BV17x411w7KC", page=2, cid="999"), [])
        exact = match_tracks(catalog, bvid="BV17x411w7KC", page=2, duration=10)
        self.assertTrue(exact[0]["match"]["eligible_for_selection"])
        mismatch = match_tracks(catalog, bvid="BV17x411w7KC", page=2, duration=50)
        self.assertEqual(mismatch[0]["match"]["alignment"], "duration_mismatch")
        self.assertFalse(mismatch[0]["match"]["eligible_for_selection"])
        self.assertEqual(match_tracks(catalog, video_id="other-edition"), [])

    def test_canonical_web_binding_preserves_content_query_but_not_tracking(self):
        url = "https://EXAMPLE.org:443/watch?v=episode2&utm_source=share&lang=zh#/player"
        self.assertEqual(canonical_url(url), "https://example.org/watch?lang=zh&v=episode2#/player")
        publish_run(self.run, self.library, bindings=[{"site": "web", "url": url}])
        catalog = load_catalog(self.library)
        self.assertTrue(match_tracks(catalog, url="https://example.org/watch?lang=zh&v=episode2#/player")[0]["match"]["identity_exact"])
        self.assertEqual(match_tracks(catalog, url="https://example.org/watch?v=episode1"), [])
        self.assertNotEqual(canonical_url("https://example.org/#/watch/1"),
                            canonical_url("https://example.org/#/watch/2"))
        for invalid in ("file:///private/video", "https://name:secret@example.org/watch",
                        "https://example.org/watch?access_token=secret", "https://example.org/watch?X-Amz-Signature=secret",
                        "https://example.org/#access_token=secret", "https://example.org/#/watch/1?token=secret"):
            with self.assertRaises(ValueError):
                canonical_url(invalid)

    def test_sidecar_uses_last_extension_and_preserves_existing_other_comments(self):
        output = publish_run(self.run, self.library, timeline={"offset": 1})
        track = self.library / output["track"]["formats"]["json"]["href"]
        exports = self.root / "sidecars"
        result = write_sidecar(track, "Series.S01E01.1080p.mkv", out_dir=exports)
        path = Path(result["path"])
        self.assertEqual(path.name, "Series.S01E01.1080p.xml")
        first = ET.parse(path).getroot().find("d")
        self.assertEqual(float(first.attrib["p"].split(",")[0]), 2)
        write_sidecar(track, "Series.S01E01.1080p.mkv", out_dir=exports)
        path.write_text("existing human comments")
        with self.assertRaises(ValueError):
            write_sidecar(track, "Series.S01E01.1080p.mkv", out_dir=exports)
        self.assertEqual(path.read_text(), "existing human comments")
        write_sidecar(track, "Series.S01E01.1080p.mkv", out_dir=exports, replace=True)
        self.assertEqual(len(ET.parse(path).getroot().findall("d")), 3)
        for video_name, xml_name in (("movie", "movie.xml"), (".hidden", ".hidden.xml"), ("movie.", "movie.xml")):
            self.assertEqual(Path(write_sidecar(track, video_name, out_dir=exports)["path"]).name, xml_name)

    def test_invalid_data_and_paths_are_rejected_before_publication(self):
        for options in ({"track_id": "../private"}, {"timeline": {"scale": 0}}, {"timeline": {"offset": float("nan")}},
                        {"bindings": [{"site": "bilibili", "bvid": "BV17x411w7KC", "page": 0}]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                publish_run(self.run, self.library, **options)
        self.assertFalse((self.library / "catalog.json").exists())
        invalid = deepcopy(self.result)
        invalid["comments"][0]["time"] = float("inf")
        with self.assertRaises(ValueError):
            make_track(invalid)


class PublishedHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "runs" / "example"
        self.run.mkdir(parents=True)
        (self.run / "comments.json").write_text(json.dumps(sample()))
        self.web = self.root / "web"
        self.web.mkdir()
        self.library = self.root / "published"
        self.published = publish_run(self.run, self.library)
        (self.library / "secret.txt").write_text("not published")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(PreviewHandler,
            runs_dir=self.run.parent, web_dir=self.web, library_dir=self.library))
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET"):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_published_bytes_and_cors_work_without_exposing_runs(self):
        status, headers, body = self.request("/catalog/catalog.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(body, (self.library / "catalog.json").read_bytes())
        href = self.published["track"]["formats"]["json"]["href"]
        status, headers, body = self.request("/catalog/" + href)
        self.assertEqual(status, 200)
        self.assertEqual(body, (self.library / href).read_bytes())
        self.assertNotIn("Access-Control-Allow-Origin", self.request("/api/runs/example")[1])
        self.assertEqual(self.request("/catalog/" + href, "HEAD")[2], b"")

    def test_nonprotocol_files_traversal_and_symlinks_are_not_served(self):
        for path in ("/catalog/secret.txt", "/catalog/../runs/example/comments.json", "/catalog/%2e%2e/secret.txt", "/catalog/.catalog.lock"):
            self.assertEqual(self.request(path)[0], 404)
        path = self.library / self.published["track"]["formats"]["json"]["href"]
        path.unlink()
        path.symlink_to(self.library / "secret.txt")
        self.assertEqual(self.request("/catalog/" + self.published["track"]["formats"]["json"]["href"])[0], 404)

    def test_catalog_only_does_not_serve_preview_runs_or_video(self):
        self.server.RequestHandlerClass = partial(PreviewHandler, runs_dir=self.run.parent,
            web_dir=self.web, library_dir=self.library, catalog_only=True)
        self.assertEqual(self.request("/catalog/catalog.json")[0], 200)
        for path in ("/", "/app.js", "/api/runs", "/api/runs/example", "/api/runs/example/video", "/api/runs/example/export.json"):
            self.assertEqual(self.request(path)[0], 404)


class PublicExampleContractTests(unittest.TestCase):
    def test_public_catalog_files_match_hashes_counts_and_preserve_base_comments(self):
        directory = Path(__file__).resolve().parents[1] / "examples" / "catalog"
        catalog = load_catalog(directory)
        documents = {}
        for item in catalog["items"]:
            for track in item["tracks"]:
                for extension, artifact in track["formats"].items():
                    path = directory / artifact["href"]
                    self.assertTrue(path.resolve().is_relative_to(directory.resolve()))
                    payload = path.read_bytes()
                    self.assertEqual(len(payload), artifact["bytes"])
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), artifact["sha256"])
                    if extension == "json":
                        document = json.loads(payload)
                        self.assertEqual(document["format"], "danmaku-track/v1")
                        self.assertTrue(document["metadata"]["synthetic"])
                        self.assertEqual(len(document["comments"]), artifact["comment_count"])
                        documents[track["id"]] = document
                    elif extension == "xml":
                        self.assertEqual(len(ET.fromstring(payload).findall("d")), artifact["comment_count"])
        self.assertEqual(set(documents), {"standard", "social"})
        base = documents["standard"]["comments"]
        social = documents["social"]["comments"]
        self.assertEqual(base, [c for c in social if c.get("generation_method") != "interaction"])
        by_id = {c["id"]: c for c in social}
        for reply in [c for c in social if c.get("generation_method") == "interaction"]:
            self.assertEqual(reply["depth"], 1)
            self.assertLess(by_id[reply["reply_to"]]["time"], reply["time"])


if __name__ == "__main__":
    unittest.main()
