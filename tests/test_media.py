"""Timestamp and partial-failure regressions, with media/model I/O mocked."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from danmaku_sim import cli, media
from danmaku_sim.costs import summarize
from danmaku_sim.providers import ProviderError, canonical


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

    def test_stt_selects_the_first_audio_stream_checked_by_probe(self):
        # A later 5.1 track can win ffmpeg's automatic audio selection even
        # though probe() validated the first, stereo track's timestamp offset.
        metadata = {"format": {"duration": "20", "start_time": "0"}, "streams": [
            {"codec_type": "video", "start_time": "0", "width": 320, "height": 180},
            {"codec_type": "audio", "start_time": "0.016", "channels": 2},
            {"codec_type": "audio", "start_time": "1.0", "channels": 6},
        ]}
        with patch("danmaku_sim.media.subprocess.run", return_value=Mock(stdout=json.dumps(metadata))):
            info = media.probe(self.source)
        self.assertEqual(info["audio_start_offset"], .016)
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(b'{"text":"","units":[]}')
        config = {"stt": {"type": "minimax", "base_url": "https://models.invalid",
                          "api_key_env": "DANMAKU_UNIT_TEST_KEY", "chunk_seconds": 10}}

        def extract(arguments):
            self.assertEqual(arguments.count("-map"), 1)
            self.assertEqual(arguments[arguments.index("-map") + 1], "0:a:0")
            self.assertGreater(arguments.index("-map"), arguments.index("-i"))
            path = Path(arguments[-1])
            path.write_bytes(("test first-audio stream " + path.name).encode())
            return Mock(stderr="")

        with patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"}), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=extract) as ffmpeg, \
             patch("danmaku_sim.media.urllib.request.build_opener", return_value=opener):
            transcript, status = media.transcribe(self.source, info, config, Mock(), self.root / "multi-audio-stt")
        self.assertEqual(transcript, [])
        self.assertEqual(ffmpeg.call_count, 2)
        self.assertEqual(status["calls"], 2)

    def test_legacy_auto_selected_wav_is_not_reused_but_identical_asr_response_is(self):
        workdir = self.root / "legacy-stt"
        workdir.mkdir()
        legacy = workdir / "chunk-0000.wav"
        legacy.write_bytes(b"legacy automatically selected surround track")
        first_track_audio = b"identical previously transcribed first-track PCM bytes"
        config = {"stt": {"type": "minimax", "base_url": "https://models.invalid",
                          "api_key_env": "DANMAKU_UNIT_TEST_KEY", "language": "zh"}}
        identity = {"version": 1, "audio": hashlib.sha256(first_track_audio).hexdigest(),
                    "language": "zh", "model": "asr-1.0", "base_url": config["stt"]["base_url"]}
        digest = hashlib.sha256(canonical(identity).encode()).hexdigest()
        document = {"text": "A cached first-track test sentence.", "units": [
            {"start": 1, "end": 2, "text": "A cached first-track test sentence."}]}
        (workdir / f"response-{digest}.json").write_text(json.dumps(document))
        records = []
        client = Mock()
        client.record.side_effect = records.append

        def extract(arguments):
            self.assertEqual(arguments[arguments.index("-map") + 1], "0:a:0")
            target = Path(arguments[-1])
            self.assertEqual(target.name, "chunk-a0-0000.wav")
            target.write_bytes(first_track_audio)
            return Mock(stderr="")

        with patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"}), \
             patch("danmaku_sim.media.run_ffmpeg", side_effect=extract) as ffmpeg, \
             patch("danmaku_sim.media.urllib.request.build_opener") as network:
            first, status = media.transcribe(self.source, {**self.info, "duration": 5}, config, client, workdir)
            reused, reused_status = media.transcribe(self.source, {**self.info, "duration": 5}, config, client, workdir)
        self.assertEqual(legacy.read_bytes(), b"legacy automatically selected surround track")
        self.assertEqual(first[0]["text"], document["text"])
        self.assertEqual(reused, first)
        self.assertEqual(status["calls"], 0)
        self.assertEqual(reused_status["calls"], 0)
        self.assertEqual(ffmpeg.call_count, 1)
        network.assert_not_called()
        self.assertEqual([row["status"] for row in records], ["local_cache_hit", "local_cache_hit"])

    def test_invalid_stt_timestamp_level_fails_before_extraction_or_upload(self):
        for level in ("segment", "", None, 1, []):
            with self.subTest(level=level), \
                 patch("danmaku_sim.media.run_ffmpeg") as ffmpeg, \
                 patch("danmaku_sim.media.urllib.request.build_opener") as network:
                with self.assertRaisesRegex(ValueError, "timestamp_level"):
                    media.transcribe(self.source, self.info,
                                     {"stt": {"type": "minimax", "timestamp_level": level}},
                                     Mock(), self.root / "invalid-level")
                ffmpeg.assert_not_called()
                network.assert_not_called()

    def test_word_timestamps_isolate_response_cache_and_reuse_first_track_wav(self):
        workdir = self.root / "timestamp-stt"
        workdir.mkdir()
        audio = workdir / "chunk-a0-0000.wav"
        audio.write_bytes(b"same audio used for sentence and word timestamps")
        settings = {"type": "minimax", "base_url": "https://models.invalid",
                    "api_key_env": "DANMAKU_UNIT_TEST_KEY", "language": "zh"}
        legacy_identity = {"version": 1, "audio": hashlib.sha256(audio.read_bytes()).hexdigest(),
                           "language": "zh", "model": "asr-1.0", "base_url": settings["base_url"]}
        legacy_digest = hashlib.sha256(canonical(legacy_identity).encode()).hexdigest()
        sentence = {"text": "门开了快走", "units": [{"start": .5, "end": 4.6, "text": "门开了快走"}]}
        (workdir / f"response-{legacy_digest}.json").write_text(json.dumps(sentence))
        words = {"text": "门开了快走", "segments": [
            {"start": .5, "end": 1, "text": "门"}, {"start": 1, "end": 1.2, "text": "开"},
            {"start": 1.2, "end": 1.6, "text": "了"}, {"start": 3.8, "end": 4.2, "text": "快"},
            {"start": 4.2, "end": 4.6, "text": "走"}]}
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(words).encode())
        records = []
        client = Mock()
        client.record.side_effect = records.append
        info = {**self.info, "duration": 5}
        with patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"}), \
             patch("danmaku_sim.media.run_ffmpeg") as ffmpeg, \
             patch("danmaku_sim.media.urllib.request.build_opener", return_value=opener):
            old, old_status = media.transcribe(self.source, info, {"stt": settings}, client, workdir)
            word_config = {"stt": {**settings, "timestamp_level": "word"}}
            grouped, fresh_status = media.transcribe(self.source, info, word_config, client, workdir)
            reused, reused_status = media.transcribe(self.source, info, word_config, client, workdir)
            explicit, explicit_status = media.transcribe(
                self.source, info, {"stt": {**settings, "timestamp_level": "sentence"}}, client, workdir)
        ffmpeg.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertIn(b'name="timestamp_level"\r\n\r\nword\r\n', request.data)
        self.assertIn(b'name="response_format"\r\n\r\nverbose_json\r\n', request.data)
        self.assertEqual([row["text"] for row in grouped], ["门开了", "快走"])
        self.assertTrue(all(row["end"] - row["start"] < 4 for row in grouped))
        self.assertEqual(reused, grouped)
        self.assertEqual(explicit, old)
        self.assertEqual([old_status["calls"], fresh_status["calls"], reused_status["calls"], explicit_status["calls"]], [0, 1, 0, 0])
        self.assertEqual(fresh_status["timestamp_level"], "word")
        self.assertEqual(len(list(workdir.glob("response-*.json"))), 2)
        self.assertEqual(records[0]["cache_key"], records[-1]["cache_key"])
        self.assertNotEqual(records[0]["cache_key"], records[1]["cache_key"])

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
        for call in opener.open.call_args_list:
            self.assertIn(b'name="timestamp_level"\r\n\r\nsentence\r\n', call.args[0].data)
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
