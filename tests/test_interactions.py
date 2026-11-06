"""Interaction constraints using explicit test doubles, never real model calls."""
from __future__ import annotations

import copy
import json
import threading
import unittest

from danmaku_sim.engine import generate
from danmaku_sim.interactions import add_interactions


def fixture(count=50, interval=10):
    config = {
        "providers": {"p_a": {"model": "test-a"}, "p_b": {"model": "test-b"}},
        "audience": [{"id": "a", "provider": "p_a", "description": "测试角色甲"},
                     {"id": "b", "provider": "p_b", "description": "测试角色乙"}],
        "generation": {"seed": 42, "history_seconds": 45, "checkpoint_mode": "events",
                       "rounds": 1, "concurrency": 2, "comments_per_window": 1},
        "scheduler": {"max_per_second": 3, "repeat_ratio": 0, "max_repeat_copies": 2,
                      "reaction_min": .35, "reaction_max": .35, "max_delay": 8},
        "interactions": {"enabled": True},
    }
    video = {"id": "test-video", "duration": count * interval + 20,
             "title": "不得泄漏的片名与未来剧情"}
    events, comments = [], []
    for i in range(count):
        at, persona = i * interval, "a" if i % 2 == 0 else "b"
        events.append({"id": f"e{i}", "start": at, "end": at, "available_at": at,
                       "text": f"仅到第{i}幕可见的测试标记", "kind": "visual", "salience": .5})
        comments.append({"id": f"base_{i:03}", "time": at + 2, "knowledge_cutoff": at + 1,
                         "event_available_at": at, "event_id": f"e{i}", "text": f"问题{i}？",
                         "persona": persona, "provider": f"p_{persona}", "model": f"test-{persona}",
                         "virtual_viewer_id": f"{persona}:round:1", "kind": "question",
                         "generation_method": "independent", "synthetic": True,
                         "mode": 1, "color": 16777215, "size": 25, "end_time": video["duration"]})
    analysis = {"schema_version": 1, "video": video, "events": events,
                "summary": "不得传给互动模型的全局结局"}
    result = {"schema_version": 1, "video": dict(video), "comments": comments,
              "candidates": [{"id": c["id"], "text": c["text"]} for c in comments],
              "diagnostics": {"status": "completed", "generation_status": "completed",
                              "comments_generated": count}}
    return analysis, config, result


class RecordingClient:
    def __init__(self, reply=None, *, fail=False, fail_first=False, cache_hit=False):
        self.reply = reply
        self.fail = fail
        self.fail_first = fail_first
        self.cache_hit = cache_hit
        self.calls, self.invalidated = [], []
        self.lock = threading.Lock()

    def complete(self, alias, system, common, variant, **kwargs):
        if not isinstance(common, str) or not isinstance(variant, str):
            raise TypeError("String context contract")
        call = {"alias": alias, "system": system, "common": common, "variant": variant,
                "context": json.loads(common), "viewer": json.loads(variant), **kwargs}
        with self.lock:
            self.calls.append(call)
            index = sum(c["stage"] == "interaction" for c in self.calls) - 1
        if kwargs["stage"] == "interaction":
            if self.fail or (self.fail_first and index == 0):
                raise RuntimeError("private-provider-error")
            value = self.reply(call) if self.reply else {"comments": [{
                "text": "也有点像吧", "interaction_type": "agreement",
                "evidence_event_ids": [call["viewer"]["parent"]["event_id"]]}]}
        else:
            target = call["context"]["target_event_ids"][0]
            value = {"comments": [{"event_id": target,
                                   "text": f"怎么回事{target}{call['viewer']['persona']['id']}？",
                                   "kind": "question"}]}
        return {"text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                "model": "test-model", "cache_key": kwargs["cache_key"],
                "local_cache_hit": self.cache_hit}

    def invalidate(self, key):
        with self.lock:
            self.invalidated.append(key)


def replies(result):
    return [c for c in result["comments"] if c.get("generation_method") == "interaction"]


class InteractionTests(unittest.TestCase):
    def test_disabled_pass_leaves_result_and_calls_unchanged(self):
        analysis, config, result = fixture()
        config.pop("interactions")
        client = RecordingClient(fail=True)
        before = copy.deepcopy(result)
        self.assertEqual(add_interactions(analysis, config, client, result), before)
        self.assertEqual(result, before)
        self.assertEqual(client.calls, [])

    def test_prefixes_contain_one_visible_parent_and_no_future_information(self):
        analysis, config, result = fixture()
        config["interactions"]["history_seconds"] = 5
        original = copy.deepcopy((analysis, config, result))
        client = RecordingClient()
        output = add_interactions(analysis, config, client, result)
        self.assertEqual((analysis, config, result), original)
        self.assertEqual(len(replies(output)), 3)
        by_id = {c["id"]: c for c in result["comments"]}
        for call in client.calls:
            self.assertEqual(call["stage"], "interaction")
            context, variant = call["context"], call["viewer"]
            cutoff, parent = context["checkpoint"], variant["parent"]
            self.assertAlmostEqual(cutoff, parent["time"] + .4)
            self.assertLess(parent["time"], cutoff)
            self.assertNotEqual(variant["persona"]["id"], by_id[parent["id"]]["persona"])
            self.assertNotIn("不得泄漏", call["common"])
            self.assertNotIn("全局结局", call["common"])
            self.assertEqual(set(variant), {"persona", "parent", "sample_seed"})
            for event in context["events"]:
                self.assertLessEqual(event["available_at"], cutoff)
                self.assertLessEqual(event["end"], cutoff)
                self.assertGreaterEqual(event["available_at"], cutoff - 5)
            for comment in result["comments"]:
                if comment["time"] > cutoff:
                    self.assertNotIn(comment["text"], call["common"] + call["variant"])
        for reply in replies(output):
            parent = by_id[reply["reply_to"]]
            self.assertEqual(reply["parent_id"], parent["id"])
            self.assertEqual(reply["parent_text"], parent["text"])
            self.assertEqual(reply["parent_time"], parent["time"])
            self.assertEqual(reply["depth"], 1)
            self.assertGreaterEqual(reply["time"] + 1e-9, reply["knowledge_cutoff"] + .35)
            self.assertGreaterEqual(reply["time"] + 1e-9, parent["time"] + .75)
            self.assertLessEqual(reply["time"], parent["time"] + 3 + 1e-9)
            self.assertLessEqual(reply["source_event"]["available_at"], reply["knowledge_cutoff"])
        self.assertEqual(output["candidates"], result["candidates"])

    def test_budget_cap_spacing_and_no_second_level_or_duplicate_threads(self):
        analysis, config, result = fixture(100)
        first = RecordingClient()
        output = add_interactions(analysis, config, first, result)
        added = replies(output)
        self.assertEqual(len(first.calls), 4)
        self.assertEqual(len(added), 4)
        self.assertEqual(len({c["reply_to"] for c in added}), 4)
        self.assertEqual(len({c["event_id"] for c in added}), 4)
        times = sorted(c["time"] for c in added)
        self.assertTrue(all(b - a >= 8 - 1e-9 for a, b in zip(times, times[1:])))
        second = RecordingClient(fail=True)
        again = add_interactions(analysis, config, second, output)
        self.assertEqual(again["comments"], output["comments"])
        self.assertEqual(second.calls, [])
        self.assertTrue(all(c["reply_to"].startswith("base_") for c in added))

    def test_small_budget_and_same_event_limit(self):
        analysis, config, result = fixture(16)
        client = RecordingClient()
        self.assertFalse(replies(add_interactions(analysis, config, client, result)))
        self.assertEqual(client.calls, [])
        analysis, config, result = fixture(50)
        config["interactions"]["history_seconds"] = 1000
        for comment in result["comments"]:
            comment["event_id"] = "e0"
        client = RecordingClient()
        output = add_interactions(analysis, config, client, result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(replies(output)), 1)

    def test_originals_and_echoes_count_toward_density_without_moving(self):
        analysis, config, result = fixture(20)
        for comment in result["comments"][1:]:
            comment["kind"] = "detail"
        parent = result["comments"][0]
        for i, at in enumerate((2.2, 2.3)):
            result["comments"].append({**parent, "id": f"echo_{i}", "time": at, "kind": "echo",
                                       "generation_method": "controlled_repeat", "parent_id": parent["id"]})
        before = copy.deepcopy(result["comments"])
        output = add_interactions(analysis, config, RecordingClient(), result)
        added = replies(output)
        self.assertEqual(len(added), 1)
        self.assertGreaterEqual(added[0]["time"], 3)
        original_by_id = {c["id"]: c for c in output["comments"] if c["generation_method"] != "interaction"}
        self.assertEqual(original_by_id, {c["id"]: c for c in before})
        times = sorted(c["time"] for c in output["comments"])
        self.assertTrue(all(times[i + 3] - times[i] >= 1 - 1e-9 for i in range(len(times) - 3)))

    def test_no_paid_request_without_density_or_target_rate_capacity(self):
        analysis, config, result = fixture(20)
        config["scheduler"]["max_per_second"] = 1
        for comment in result["comments"][1:]:
            comment["kind"] = "detail"
        parent = result["comments"][0]
        for i, at in enumerate((3, 4, 5)):
            result["comments"].append({**parent, "id": f"occupied_{i}", "time": at, "kind": "echo",
                                       "generation_method": "controlled_repeat", "parent_id": parent["id"]})
        client = RecordingClient()
        output = add_interactions(analysis, config, client, result)
        self.assertEqual(client.calls, [])
        self.assertEqual(output["diagnostics"]["interactions"]["skipped"]["no_density_or_spacing_slot"], 1)
        analysis, config, result = fixture()
        config["scheduler"]["target_per_minute"] = 0
        self.assertFalse(replies(add_interactions(analysis, config, client, result)))
        self.assertEqual(client.calls, [])

    def test_no_other_viewer_and_near_end_skip_without_calling(self):
        analysis, config, result = fixture(20)
        config["audience"] = config["audience"][:1]
        for comment in result["comments"]:
            comment["persona"] = "a"
        client = RecordingClient()
        output = add_interactions(analysis, config, client, result)
        self.assertEqual(client.calls, [])
        self.assertEqual(output["diagnostics"]["interactions"]["skipped"]["no_other_viewer"], 20)
        analysis, config, result = fixture(20)
        for comment in result["comments"][1:]:
            comment["kind"] = "detail"
        result["comments"][0]["time"] = analysis["video"]["duration"] - .1
        output = add_interactions(analysis, config, client, result)
        self.assertEqual(client.calls, [])
        self.assertEqual(output["diagnostics"]["interactions"]["skipped"]["no_time_remaining"], 1)

    def test_silence_does_not_cause_extra_calls_to_fill_budget(self):
        analysis, config, result = fixture(100)
        client = RecordingClient(reply=lambda c: {"comments": []})
        output = add_interactions(analysis, config, client, result)
        self.assertEqual(len(client.calls), 4)
        self.assertFalse(replies(output))
        self.assertEqual(output["comments"], result["comments"])
        self.assertEqual(output["diagnostics"]["interactions"]["silent_calls"], 4)
        self.assertEqual(output["diagnostics"]["status"], "completed")

    def test_partial_or_all_failure_keeps_every_existing_comment(self):
        analysis, config, result = fixture(100)
        for client, expected in ((RecordingClient(fail_first=True), 3), (RecordingClient(fail=True), 0),
                                 (RecordingClient(reply=lambda c: "bad JSON"), 0)):
            with self.subTest(expected=expected):
                output = add_interactions(analysis, config, client, result)
                self.assertEqual(len(replies(output)), expected)
                originals = [c for c in output["comments"] if c["generation_method"] != "interaction"]
                self.assertEqual(originals, result["comments"])
                self.assertEqual(output["diagnostics"]["status"], "partial")
                self.assertEqual(output["diagnostics"]["generation_status"], "completed")
                self.assertEqual(len(client.calls), 4)
                self.assertNotIn("private-provider-error", json.dumps(output))

    def test_uncertain_evidence_needs_hedged_correction(self):
        analysis, config, result = fixture(20)
        for event in analysis["events"]:
            event["uncertain"] = True
        def reply(call, text):
            return {"comments": [{"text": text, "interaction_type": "correction",
                                   "evidence_event_ids": [call["viewer"]["parent"]["event_id"]]}]}
        rejected = RecordingClient(reply=lambda c: reply(c, "这是确定的"))
        output = add_interactions(analysis, config, rejected, result)
        self.assertFalse(replies(output))
        self.assertEqual(output["diagnostics"]["interactions"]["filtered"]["unqualified_uncertain_correction"], 1)
        self.assertEqual(len(rejected.invalidated), 1)
        accepted = add_interactions(analysis, config, RecordingClient(reply=lambda c: reply(c, "也不一定吧")), result)
        self.assertEqual(len(replies(accepted)), 1)
        self.assertTrue(replies(accepted)[0]["source_event"]["uncertain"])

    def test_future_evidence_wrong_parent_and_copied_text_are_rejected(self):
        analysis, config, result = fixture(20)
        at = analysis["video"]["duration"] - 1
        analysis["events"].append({"id": "future", "start": at, "end": at, "available_at": at,
                                   "text": "尚未出现的未来证据", "salience": 1})
        variants = [
            ("unknown_or_future_evidence", lambda c: {"text": "看到了", "evidence_event_ids": ["future"]}),
            ("wrong_parent", lambda c: {"text": "看到了", "reply_to": "another"}),
            ("copied_parent", lambda c: {"text": c["viewer"]["parent"]["text"]}),
        ]
        for reason, change in variants:
            with self.subTest(reason=reason):
                def reply(call):
                    row = {"text": "看到了", "interaction_type": "reply",
                           "evidence_event_ids": [call["viewer"]["parent"]["event_id"]]}
                    row.update(change(call))
                    return {"comments": [row]}
                client = RecordingClient(reply=reply)
                output = add_interactions(analysis, config, client, result)
                self.assertFalse(replies(output))
                self.assertEqual(output["diagnostics"]["status"], "partial")
                self.assertEqual(output["diagnostics"]["interactions"]["filtered"][reason], 1)
                self.assertNotIn("尚未出现的未来证据", client.calls[0]["common"])

    def test_one_reply_per_parent_even_if_model_returns_multiple(self):
        analysis, config, result = fixture(20)
        def many(call):
            return {"comments": [{"text": text, "interaction_type": "agreement",
                                   "evidence_event_ids": [call["viewer"]["parent"]["event_id"]]}
                                  for text in ("也有点像吧", "有道理", "确实") ]}
        output = add_interactions(analysis, config, RecordingClient(reply=many), result)
        self.assertEqual(len(replies(output)), 1)
        self.assertEqual(output["diagnostics"]["interactions"]["filtered"]["per_parent_limit"], 2)

    def test_cache_keys_and_replay_scheduling_are_deterministic(self):
        analysis, config, result = fixture(50)
        clients = [RecordingClient(), RecordingClient(cache_hit=True)]
        outputs = [add_interactions(analysis, config, client, result) for client in clients]
        keys = [sorted(c["cache_key"] for c in client.calls) for client in clients]
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(all(len(key) == 64 for key in keys[0]))
        self.assertEqual(outputs[0]["comments"], outputs[1]["comments"])
        self.assertEqual(outputs[0]["interaction_candidates"], outputs[1]["interaction_candidates"])
        self.assertEqual(outputs[1]["diagnostics"]["interactions"]["local_cache_hits"], 3)

    def test_engine_integration_preserves_independent_calls_and_candidates(self):
        analysis, config, _ = fixture(20)
        disabled = copy.deepcopy(config)
        disabled["interactions"]["enabled"] = False
        base_client, enabled_client = RecordingClient(), RecordingClient()
        baseline = generate(analysis, disabled, base_client)
        extended = generate(analysis, config, enabled_client)
        original_calls = lambda client: sorted((c["alias"], c["common"], c["variant"], c["cache_key"])
                                               for c in client.calls if c["stage"] == "generation")
        self.assertEqual(original_calls(base_client), original_calls(enabled_client))
        self.assertEqual(baseline["candidates"], extended["candidates"])
        self.assertEqual(baseline["comments"], [c for c in extended["comments"] if c["generation_method"] != "interaction"])
        self.assertGreater(len(replies(extended)), 0)
        self.assertNotIn("interactions", baseline["diagnostics"])

    def test_invalid_configuration_fails_before_any_generation_cost(self):
        analysis, config, _ = fixture(20)
        for key, value in [("enabled", "true"), ("ratio", 1.01), ("max_depth", 2),
                           ("read_delay", 0), ("max_parent_delay", .5), ("history_seconds", 0)]:
            with self.subTest(key=key):
                changed = copy.deepcopy(config)
                changed["interactions"][key] = value
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    generate(analysis, changed, client)
                self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
