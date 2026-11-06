"""Timestamp and partial-failure regressions, with media/model I/O mocked."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from danmaku_sim import cli, media
from danmaku_sim.costs import summarize
from danmaku_sim.providers import ProviderError


class MediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"test-only media")
        self.info = {"duration": 20.0, "width": 320, "height": 180, "has_audio": True,
                     "audio_start_offset": 0, "video_start_offset": 0}

    def test_end_tolerance_cannot_create_a_reversed_segment(self):
        with self.assertRaises(ValueError):
            media.normalize_transcript([{"start": 20.1, "end": 20.2, "text": "outside"}], 20)
        segments = media.normalize_transcript(
            [{"start": 19, "end": 20.2, "text": "A complete ending sentence."}], 20)
        self.assertEqual((segments[0]["start"], segments[0]["end"]), (19, 20))

    def test_unaligned_audio_is_rejected_before_extracting_or_uploading(self):
        info = {**self.info, "audio_start_offset": 2}
        with patch("danmaku_sim.media.run_ffmpeg") as ffmpeg:
            with self.assertRaises(ValueError):
                media.transcribe(self.source, info, {}, Mock(), self.root / "stt")
            ffmpeg.assert_not_called()

    def test_partial_vision_failure_preserves_speech(self):
        transcript = self.root / "transcript.json"
        transcript.write_text(json.dumps([{
            "start": 1, "end": 3, "text": "This is a complete test sentence.",
        }]))
        for failure in ("bad_visual_shape", "missing_extracted_frame"):
            with self.subTest(failure=failure):
                run = self.root / failure
                client = Mock()
                client.complete.return_value = {"text": "[]", "model": "test"}

                def extract(arguments):
                    if failure != "missing_extracted_frame":
                        Path(arguments[-1]).write_bytes(b"test-only frame")
                    return Mock(stderr="")

                with patch("danmaku_sim.media.probe", return_value=self.info), \
                     patch("danmaku_sim.media.frame_plan",
                           return_value=([{"time": 5, "reason": "test"}], [])), \
                     patch("danmaku_sim.media.run_ffmpeg", side_effect=extract):
                    result = media.analyze(
                        self.source, run, {"analysis": {"vision_provider": "test"}}, client,
                        transcript_path=transcript)
                self.assertEqual(result["diagnostics"]["status"], "partial")
                self.assertEqual(result["diagnostics"]["frames_analyzed"], 0)
                self.assertEqual(len(result["events"]), 1)
                self.assertEqual(result["events"][0]["available_at"], 3)
                self.assertEqual(result["events"][0]["kind"], "speech")
                self.assertTrue((run / "analysis.json").is_file())

    def test_no_evidence_cannot_be_reported_as_success(self):
        client = Mock()
        client.complete.side_effect = ProviderError("test provider failed")

        def extract(arguments):
            Path(arguments[-1]).write_bytes(b"test-only frame")
            return Mock(stderr="")

        with patch("danmaku_sim.media.probe", return_value=self.info), \
             patch("danmaku_sim.media.frame_plan",
                   return_value=([{"time": 5, "reason": "test"}], [])), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=extract):
            with self.assertRaises(ValueError):
                media.analyze(self.source, self.root / "empty",
                              {"analysis": {"vision_provider": "test"}}, client, no_stt=True)

    def test_stt_chunk_times_and_overlap_cost_survive_cache_reuse(self):
        documents = iter([
            {"text": "First complete test sentence.",
             "units": [{"start": 2, "end": 3, "text": "First complete test sentence."}]},
            {"text": "Second complete test sentence.",
             "units": [{"start": 2, "end": 3, "text": "Second complete test sentence."}]},
        ])
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(next(documents)).encode())
        client = Mock()
        records = []
        client.record.side_effect = records.append
        config = {"stt": {
            "type": "minimax", "base_url": "https://models.invalid",
            "api_key_env": "DANMAKU_UNIT_TEST_KEY", "chunk_seconds": 10,
            "price_per_hour": .36, "billing_mode": "payg",
        }}

        def extract(arguments):
            path = Path(arguments[-1])
            path.write_bytes(("test audio " + path.name).encode())
            return Mock(stderr="")

        with patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"}), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=extract), \
             patch("danmaku_sim.media.urllib.request.build_opener", return_value=opener):
            first, first_status = media.transcribe(
                self.source, self.info, config, client, self.root / "stt")
            reused, reused_status = media.transcribe(
                self.source, self.info, config, client, self.root / "stt")
        self.assertEqual([(s["start"], s["end"]) for s in first], [(2, 3), (10.5, 11.5)])
        self.assertEqual(reused, first)
        self.assertEqual(first_status["calls"], 2)
        self.assertEqual(reused_status["calls"], 0)
        self.assertEqual(opener.open.call_count, 2)
        summary = summarize(records)
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["local_cache_hits"], 2)
        self.assertEqual(summary["audio_seconds"], 23)
        self.assertAlmostEqual(summary["estimated_cost"], .0023)

    def test_stt_cache_write_failure_does_not_invent_a_second_request(self):
        info = {**self.info, "duration": 5.0}
        document = {"text": "One complete sentence.",
                    "units": [{"start": 1, "end": 2, "text": "One complete sentence."}]}
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(document).encode())
        records = []
        client = Mock()
        client.record.side_effect = records.append
        config = {"stt": {
            "type": "minimax", "base_url": "https://models.invalid",
            "api_key_env": "DANMAKU_UNIT_TEST_KEY", "price_per_hour": .36,
        }}

        def extract(arguments):
            Path(arguments[-1]).write_bytes(b"test audio")
            return Mock(stderr="")

        with patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"}), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=extract), \
             patch("danmaku_sim.media.urllib.request.build_opener", return_value=opener), \
             patch("danmaku_sim.media.write_json", side_effect=OSError("test cache failure")):
            try:
                media.transcribe(self.source, info, config, client, self.root / "cache-failure")
            except (ProviderError, OSError):
                pass
        self.assertEqual(opener.open.call_count, 1)
        summary = summarize(records)
        self.assertEqual(summary["request_count"], 1)
        self.assertAlmostEqual(summary["estimated_cost"], .0005)

    def test_generate_cannot_replace_a_run_bound_to_another_video(self):
        source_dir, output = self.root / "input", self.root / "output"
        source_dir.mkdir()
        output.mkdir()
        analysis = source_dir / "analysis.json"
        analysis.write_text(json.dumps({"video": {"id": "new-video"}}))
        original_analysis = json.dumps({"video": {"id": "old-video"}})
        (output / "analysis.json").write_text(original_analysis)
        (output / "media.json").write_text(json.dumps({"path": "/private/old.mp4", "sha256": "old-video"}))
        original_config = '{"old": true}'
        (output / "config.json").write_text(original_config)
        with patch("danmaku_sim.cli.load_config", return_value={"providers": {}}), \
             patch("danmaku_sim.engine.generate") as generate, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = cli.main(["generate", "--analysis", str(analysis),
                               "--config", "unused-test-config.json", "--out", str(output)])
        self.assertEqual(status, 1)
        generate.assert_not_called()
        self.assertEqual((output / "analysis.json").read_text(), original_analysis)
        self.assertEqual((output / "config.json").read_text(), original_config)


if __name__ == "__main__":
    unittest.main()
