"""Optional background context must not invalidate legacy empty requests.

All model calls use explicit recording clients or a mocked HTTP opener.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from danmaku_sim.config import load_config
from danmaku_sim.costs import load_usage
from danmaku_sim.engine import generate
from danmaku_sim.interactions import add_interactions
from danmaku_sim.providers import ModelClient, canonical


INTRO = "基础背景：两位旧识生活在同一座城中。"
MISSING = object()
# Captured from the unchanged implementation before adding video_intro.
LEGACY_CALLS = {
    "generation": "0e5519754528bb2f796c4b062de85760b0bd67d32f909947df7476f85fb52f5d",
    "interaction": "ea83203bf35f52c5f6966c0030f92620095ccc7e5f91d6ca42fc370e3105c144",
}


def fixture():
    config = {
        "providers": {"p": {"type": "openai_chat", "base_url": "https://models.invalid/v1",
                            "model": "test-model", "api_key_env": "DANMAKU_INTRO_TEST_KEY",
                            "pricing": {"currency": "USD", "billing_mode": "payg",
                                        "input_per_million": 1, "output_per_million": 2}}},
        "audience": [{"id": "a", "provider": "p", "description": "测试观众甲"},
                     {"id": "b", "provider": "p", "description": "测试观众乙"}],
        "generation": {"checkpoint_mode": "events", "event_merge_seconds": 0,
                       "history_seconds": 45, "rounds": 1, "comments_per_window": 1,
                       "concurrency": 1, "seed": 17},
        "scheduler": {"repeat_ratio": 0, "reaction_min": .35, "reaction_max": .35,
                      "max_delay": 8, "max_per_second": 3},
        "interactions": {"enabled": False, "ratio": 1, "max_comments": 2,
                         "min_spacing_seconds": 8, "concurrency": 1},
    }
    analysis = {
        "video": {"id": "intro-test-video", "duration": 90, "title": "未请求的片名"},
        "summary": "未经请求的全局结局",
        "events": [
            {"id": "e0", "start": 1, "end": 2, "available_at": 2,
             "kind": "speech", "text": "先看看这里", "salience": .6},
            {"id": "e1", "start": 20, "end": 20, "available_at": 20,
             "kind": "visual", "text": "像是两个人在门边", "salience": .7, "uncertain": True},
            {"id": "e2", "start": 60, "end": 61, "available_at": 61,
             "kind": "speech", "text": "后面才会听到的证据", "salience": .6},
        ],
    }
    comments = []
    for i, (at, persona) in enumerate(((2, "a"), (20, "b"))):
        comments.append({"id": f"base-{i}", "time": at + 1, "knowledge_cutoff": at,
                         "event_available_at": at, "event_id": f"e{i}", "text": "这是怎么了？",
                         "persona": persona, "virtual_viewer_id": persona, "kind": "question",
                         "generation_method": "independent", "synthetic": True})
    result = {"video": dict(analysis["video"]), "comments": comments,
              "candidates": [], "diagnostics": {"status": "completed"}}
    return analysis, config, result


class RecordingClient:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows
        self.calls, self.invalidated = [], []

    def complete(self, alias, system, common, variant, **kwargs):
        self.calls.append({"provider": alias, "system": system, "common": common,
                           "variant": variant, "stage": kwargs["stage"], "cache_key": kwargs["cache_key"]})
        return {"text": json.dumps({"comments": self.rows}, ensure_ascii=False),
                "model": "test-model", "cache_key": kwargs["cache_key"]}

    def invalidate(self, key):
        self.invalidated.append(key)


def capture(stage, intro=MISSING, rows=None):
    analysis, config, base = fixture()
    if intro is not MISSING:
        config["video_intro"] = intro
    client = RecordingClient(rows)
    if stage == "generation":
        output = generate(analysis, config, client)
    else:
        config["interactions"]["enabled"] = True
        output = add_interactions(analysis, config, client, base)
    return analysis, config, base, client, output


class VideoIntroTests(unittest.TestCase):
    def test_empty_intro_keeps_exact_legacy_requests(self):
        for stage in ("generation", "interaction"):
            for intro in (MISSING, None, "", " \t\n"):
                with self.subTest(stage=stage, intro=intro):
                    _, _, _, client, output = capture(stage, intro)
                    self.assertEqual(hashlib.sha256(canonical(client.calls).encode()).hexdigest(), LEGACY_CALLS[stage])
                    diagnostics = output["diagnostics"]
                    if stage == "interaction":
                        diagnostics = diagnostics["interactions"]
                    self.assertNotIn("video_intro_enabled", diagnostics)
                    self.assertNotIn("video_intro_sha256", diagnostics)
                    for call in client.calls:
                        self.assertNotIn("video_intro", json.loads(call["common"]))

    def test_nonempty_intro_precedes_variable_context_and_preserves_evidence(self):
        for stage in ("generation", "interaction"):
            with self.subTest(stage=stage):
                old = capture(stage)[3].calls
                analysis, _, _, client, output = capture(stage, "\n " + INTRO + " \n")
                self.assertEqual(len(client.calls), len(old))
                common_by_checkpoint = {}
                for previous, call in zip(old, client.calls):
                    context = json.loads(call["common"])
                    self.assertEqual(context.pop("video_intro"), INTRO)
                    self.assertEqual(context, json.loads(previous["common"]))
                    self.assertEqual(call["variant"], previous["variant"])
                    self.assertNotEqual(call["system"], previous["system"])
                    self.assertIn("video_intro", call["system"])
                    self.assertNotEqual(call["cache_key"], previous["cache_key"])
                    self.assertLess(call["common"].index('"video_intro"'), call["common"].index('"checkpoint"'))
                    self.assertNotIn(analysis["video"]["title"], call["common"])
                    self.assertNotIn(analysis["summary"], call["common"])
                    for event in context["events"]:
                        self.assertLessEqual(event["available_at"], context["checkpoint"])
                        self.assertLessEqual(event["end"], context["checkpoint"])
                    common_by_checkpoint.setdefault(context["checkpoint"], set()).add(call["common"])
                self.assertTrue(all(len(values) == 1 for values in common_by_checkpoint.values()))
                diagnostics = output["diagnostics"]
                if stage == "interaction":
                    diagnostics = diagnostics["interactions"]
                self.assertTrue(diagnostics["video_intro_enabled"])
                self.assertEqual(diagnostics["video_intro_sha256"], hashlib.sha256(INTRO.encode()).hexdigest())

    def test_interaction_intro_audit_does_not_relabel_or_modify_base_generation(self):
        analysis, config, base = fixture()
        config["video_intro"] = INTRO
        config["interactions"]["enabled"] = True
        base["diagnostics"]["video_intro_sha256"] = hashlib.sha256(b"older base context").hexdigest()
        before = copy.deepcopy((analysis, config, base))
        result = add_interactions(analysis, config, RecordingClient(), base)
        self.assertEqual((analysis, config, base), before)
        self.assertEqual(result["comments"], base["comments"])
        self.assertEqual(result["diagnostics"]["video_intro_sha256"], base["diagnostics"]["video_intro_sha256"])
        self.assertEqual(result["diagnostics"]["interactions"]["video_intro_sha256"], hashlib.sha256(INTRO.encode()).hexdigest())

    def test_intro_cannot_be_used_as_a_target_or_interaction_evidence(self):
        *_, generated = capture("generation", INTRO, [{"event_id": "video_intro", "text": "只有背景", "kind": "reaction"}])
        self.assertEqual(generated["comments"], [])
        self.assertGreater(generated["diagnostics"]["filtered"]["unknown_or_noncurrent_event"], 0)
        _, _, base, _, social = capture("interaction", INTRO, [{"text": "应该这样吧", "interaction_type": "reply",
                                                                  "evidence_event_ids": ["video_intro"]}])
        self.assertEqual(social["comments"], base["comments"])
        self.assertEqual(social["diagnostics"]["interactions"]["status"], "partial")
        self.assertGreater(social["diagnostics"]["interactions"]["filtered"]["unknown_or_future_evidence"], 0)

    def test_invalid_intro_types_fail_before_any_request(self):
        for intro in ([], {}, True, 17):
            for stage in ("generation", "interaction"):
                with self.subTest(intro=intro, stage=stage):
                    analysis, config, base = fixture()
                    config["video_intro"] = intro
                    config["interactions"]["enabled"] = stage == "interaction"
                    client = RecordingClient()
                    with self.assertRaisesRegex(ValueError, "video_intro"):
                        if stage == "generation":
                            generate(analysis, config, client)
                        else:
                            add_interactions(analysis, config, client, base)
                    self.assertEqual(client.calls, [])

    def test_config_validates_intro_without_adding_a_default_or_changing_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (MISSING, None, "", " \n", " " + INTRO + " "):
                _, config, _ = fixture()
                if value is not MISSING:
                    config["video_intro"] = value
                path.write_text(json.dumps(config), encoding="utf-8")
                loaded = load_config(path)
                if value is MISSING:
                    self.assertNotIn("video_intro", loaded)
                else:
                    self.assertEqual(loaded["video_intro"], value)
            for value in ([], {}, True, 17):
                _, config, _ = fixture()
                config["video_intro"] = value
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "video_intro"):
                    load_config(path)

    def test_provider_cache_reuses_empty_requests_and_separates_intro_changes(self):
        for stage in ("generation", "interaction"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                requests = [capture(stage, value)[3].calls[0] for value in
                            (MISSING, " \n", INTRO, INTRO, INTRO + "新增背景。")]
                _, config, _ = fixture()
                opener = Mock()
                response = {"id": "same-provider-id", "model": "test-model",
                            "choices": [{"message": {"content": '{"comments":[]}'}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
                opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(response).encode())
                client = ModelClient(config, Path(directory) / "run", Path(directory) / "cache")
                with patch.dict(os.environ, {"DANMAKU_INTRO_TEST_KEY": "unit-test-only"}), \
                     patch("danmaku_sim.providers.urllib.request.build_opener", return_value=opener):
                    results = [client.complete(call["provider"], call["system"], call["common"], call["variant"],
                                               stage=call["stage"], cache_key=call["cache_key"]) for call in requests]
                self.assertEqual(opener.open.call_count, 3)
                self.assertEqual([r["local_cache_hit"] for r in results], [False, True, False, True, False])
                self.assertEqual(results[0]["cache_key"], results[1]["cache_key"])
                self.assertEqual(results[2]["cache_key"], results[3]["cache_key"])
                self.assertEqual(len({r["cache_key"] for r in results}), 3)
                usage = load_usage(client.run_dir)["summary"]
                self.assertEqual((usage["request_count"], usage["local_cache_hits"]), (3, 2))
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (3, 3))
                self.assertAlmostEqual(usage["estimated_cost"], .000009)


if __name__ == "__main__":
    unittest.main()
