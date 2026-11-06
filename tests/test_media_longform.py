"""Whole-film coverage and resumable bounded vision workers, with no APIs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from danmaku_sim import media
from danmaku_sim.providers import ProviderError


class CachedVision:
    """Explicit in-memory provider/cache test double; never contacts a service."""

    def __init__(self, *, barrier_size=0, failures=()):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(barrier_size) if barrier_size else None
        self.barrier_size = barrier_size
        self.failures = set(failures)
        self.cache, self.calls = {}, []
        self.fresh = self.hits = self.active = self.max_active = 0

    def complete(self, alias, system, common, variant, *, stage, images):
        assert stage == "vision"
        assert isinstance(common, str) and isinstance(variant, str)
        frame = images[0]
        key = (frame["time"], frame["sha256"])
        with self.lock:
            self.calls.append(key)
            if key in self.cache:
                self.hits += 1
                return {**self.cache[key], "local_cache_hit": True}
            self.fresh += 1
            call_number = self.fresh
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None and call_number <= self.barrier_size:
                self.barrier.wait(timeout=3)
            if frame["time"] in self.failures:
                raise ProviderError("test-only transient failure")
            response = {"text": json.dumps({"description": f"第{frame['time']}秒的测试画面",
                                             "salience": .5, "uncertain": False}, ensure_ascii=False),
                        "model": "test-model", "cache_key": hashlib.sha256(repr(key).encode()).hexdigest(),
                        "local_cache_hit": False}
            with self.lock:
                self.cache[key] = response
            return copy.deepcopy(response)
        finally:
            with self.lock:
                self.active -= 1

    def invalidate(self, key):
        pass


class LongformMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"test-only source, not a video")
        self.info = {"duration": 60, "width": 854, "height": 480, "has_audio": False,
                     "audio_start_offset": 0, "video_start_offset": 0}
        self.config = {"analysis": {"sampling_mode": "uniform", "frame_interval": 10,
                                    "max_frames": 7, "vision_concurrency": 3,
                                    "vision_provider": "test", "scene_detection": False}}

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def extract(arguments):
        path = Path(arguments[-1])
        path.write_bytes(("test-only frame " + path.name).encode())
        return Mock(stderr="")

    def analyze(self, client, config=None, output="run"):
        with patch("danmaku_sim.media.probe", return_value=self.info), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=self.extract):
            return media.analyze(self.source, self.root / output, config or self.config, client, no_stt=True)

    def test_uniform_94_minute_plan_covers_tail_without_full_scene_scan(self):
        transcript = [{"end": i + .1, "text": "看这里"} for i in range(100)]
        with patch("danmaku_sim.media.run_ffmpeg") as ffmpeg:
            plan, warnings = media.frame_plan("not-opened.mkv", 5636.842, transcript,
                {"sampling_mode": "uniform", "frame_interval": 18, "max_frames": 320,
                 "scene_detection": True})
        self.assertEqual(len(plan), 315)
        self.assertEqual(plan[0]["time"], .3)
        self.assertEqual(plan[-1]["time"], 5636.742)
        self.assertLessEqual(max(b["time"] - a["time"] for a, b in zip(plan, plan[1:])), 18)
        self.assertEqual(warnings, [])
        ffmpeg.assert_not_called()

    def test_uniform_cap_is_spread_by_time_and_old_mixed_default_remains(self):
        plan, _ = media.frame_plan("unused", 3600, [],
                                  {"sampling_mode": "uniform", "frame_interval": 6, "max_frames": 100})
        self.assertEqual(len(plan), 100)
        self.assertEqual(plan[-1]["time"], 3599.9)
        self.assertTrue(1700 < plan[49]["time"] < 1900)
        original = {"frame_interval": 4, "max_frames": 4, "scene_detection": False}
        default, _ = media.frame_plan("unused", 20, [], original)
        explicit, _ = media.frame_plan("unused", 20, [], {**original, "sampling_mode": "mixed"})
        self.assertEqual(default, explicit)
        self.assertEqual([f["time"] for f in default], [.3, 4, 12, 16])

    def test_parallel_progress_is_ordered_and_written_only_by_coordinator(self):
        client = CachedVision(barrier_size=3)
        snapshots, writer = [], media.write_json
        owner = threading.get_ident()
        def record(path, document):
            if Path(path).name == "frames.json":
                self.assertEqual(threading.get_ident(), owner)
                snapshots.append(copy.deepcopy(document))
            writer(path, document)
        with patch("danmaku_sim.media.write_json", side_effect=record):
            result = self.analyze(client)
        self.assertEqual(client.max_active, 3)
        self.assertEqual(result["diagnostics"]["frames_analyzed"], 7)
        self.assertEqual(len(snapshots), 8)
        self.assertTrue(all(f["status"] == "pending" for f in snapshots[0]))
        for completed, snapshot in enumerate(snapshots):
            self.assertEqual(sum(f["status"] == "completed" for f in snapshot), completed)
            self.assertEqual([f["time"] for f in snapshot], sorted(f["time"] for f in snapshot))
        self.assertEqual([e["available_at"] for e in result["events"]], sorted(e["available_at"] for e in result["events"]))

    def test_failed_frame_resumes_from_cache_and_concurrency_can_change(self):
        planned, _ = media.frame_plan("unused", 60, [], self.config["analysis"])
        failure_at = planned[3]["time"]
        client = CachedVision(failures={failure_at})
        first = self.analyze(client)
        self.assertEqual(first["diagnostics"]["status"], "partial")
        self.assertEqual(first["diagnostics"]["frames_analyzed"], 6)
        self.assertEqual(client.fresh, 7)
        saved = json.loads((self.root / "run" / "frames.json").read_text())
        self.assertEqual(sum(f["status"] == "failed" for f in saved), 1)
        client.failures.clear()
        slower = copy.deepcopy(self.config)
        slower["analysis"]["vision_concurrency"] = 1
        with patch("danmaku_sim.media.probe", return_value=self.info), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=AssertionError("cached frames must be reused")):
            second = media.analyze(self.source, self.root / "run", slower, client, no_stt=True)
        self.assertEqual(second["diagnostics"]["status"], "completed")
        self.assertEqual(second["diagnostics"]["frames_analyzed"], 7)
        self.assertEqual(client.fresh, 8)
        self.assertEqual(client.hits, 6)
        third = self.analyze(client)
        self.assertEqual(third, second)
        self.assertEqual(client.fresh, 8)
        self.assertEqual(client.hits, 13)

    def test_default_is_serial_and_does_not_change_output(self):
        sequential = copy.deepcopy(self.config)
        sequential["analysis"].pop("vision_concurrency")
        client = CachedVision()
        original = self.analyze(client, sequential, "serial")
        self.assertEqual(client.max_active, 1)
        parallel = self.analyze(CachedVision(barrier_size=3), self.config, "parallel")
        self.assertEqual(original, parallel)

    def test_local_progress_failure_does_not_queue_entire_film(self):
        client = CachedVision(barrier_size=3)
        writer = media.write_json
        def fail_progress(path, document):
            if Path(path).name == "frames.json" and any(f["status"] == "completed" for f in document):
                raise OSError("test-only progress write failure")
            writer(path, document)
        with patch("danmaku_sim.media.write_json", side_effect=fail_progress), self.assertRaises(OSError):
            self.analyze(client)
        self.assertLessEqual(client.fresh, 3)

    def test_invalid_parallelism_is_rejected_before_stt_or_extraction(self):
        for value in (0, 9, 1.5, True):
            with self.subTest(value=value), patch("danmaku_sim.media.probe") as probe:
                config = {"analysis": {"vision_concurrency": value}}
                with self.assertRaises(ValueError):
                    media.analyze(self.source, self.root / "invalid", config, Mock())
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
