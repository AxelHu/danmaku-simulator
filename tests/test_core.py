"""Core invariants; tests never contact an LLM or silently substitute one."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

from danmaku_sim.engine import GenerationError, generate
from danmaku_sim.exporters import escape_ass, escape_html, write_ass, write_json, write_xml
from danmaku_sim.scheduler import schedule_comments, text_key


def settings() -> dict:
    return {
        "providers": {"a": {"model": "model-a"}, "b": {"model": "model-b"}},
        "audience": [
            {"id": "casual", "provider": "a", "description": "普通观众，允许沉默"},
            {"id": "detail", "provider": "b", "description": "偶尔注意细节"},
        ],
        "generation": {"window_seconds": 8, "history_seconds": 45, "rounds": 1,
                       "comments_per_window": 2, "seed": 42, "concurrency": 3},
        "scheduler": {"reaction_min": .35, "reaction_max": .35, "max_delay": 8,
                      "max_per_second": 3, "repeat_ratio": 0, "max_repeat_copies": 2},
    }


def event(event_id: str, at: float, text: str = "眼前出现了一个细节", **kwargs) -> dict:
    return {"id": event_id, "start": at, "end": at, "available_at": at,
            "kind": "visual", "text": text, "salience": .5, **kwargs}


def analysis(events: list[dict], duration: float = 30) -> dict:
    return {"schema_version": 1, "video": {"id": "content-hash", "title": "片名里的未来秘密",
                                          "duration": duration},
            "summary": "全局摘要里的未来秘密", "events": events}


class FakeClient:
    """An explicit recording test double, not a production fallback."""

    def __init__(self, *, fail=(), reply=None):
        self.fail = set(fail)
        self.reply = reply
        self.calls = []
        self.lock = threading.Lock()

    def complete(self, alias, system, common, variant, **kwargs):
        if not isinstance(common, str) or not isinstance(variant, str):
            raise TypeError("provider contract requires string prefix and variant")
        call = {"alias": alias, "system": system, "common": common, "variant": variant,
                "context": json.loads(common), "viewer": json.loads(variant), **kwargs}
        with self.lock:
            self.calls.append(call)
        if alias in self.fail:
            raise RuntimeError("private provider error must not be copied into diagnostics")
        if self.reply:
            result = self.reply(call)
        else:
            result = {"comments": [
                {"event_id": eid, "text": f"看到{eid}啦{call['viewer']['persona']['id']}",
                 "kind": "detail"}
                for eid in call["context"]["target_event_ids"][:call["context"]["max_comments"]]]
            }
        return {"text": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                "model": f"model-{alias}", "request_id": "test-request", "usage": {}}


def candidate(index: int, cutoff: float = 1, text: str | None = None, **kwargs) -> dict:
    return {"id": f"c{index:03}", "text": text or f"反应{index}", "event_id": f"e{index}",
            "knowledge_cutoff": cutoff, "event_available_at": cutoff,
            "persona": f"p{index}", "virtual_viewer_id": f"viewer{index}", "provider": "a",
            "model": "model-a", "kind": "reaction", **kwargs}


class GenerationTests(unittest.TestCase):
    def test_only_causal_prefixes_and_full_checkpoint_cutoff(self):
        client = FakeClient()
        result = generate(analysis([
            event("early", 1), event("edge", 8), event("future", 8.0001, "后来才出现的画面"),
            event("speech", 3, "整句话到十八秒才说完", end=18, available_at=3, kind="speech"),
            event("last", 29.8),
        ]), settings(), client)
        at_eight = [c for c in client.calls if c["context"]["checkpoint"] == 8]
        self.assertTrue(at_eight)
        for call in at_eight:
            self.assertEqual(call["context"]["target_event_ids"], ["early", "edge"])
            self.assertNotIn("后来才出现", call["common"])
            self.assertNotIn("十八秒", call["common"])
        for call in client.calls:
            cp = call["context"]["checkpoint"]
            self.assertNotIn("未来秘密", call["common"])
            for observed in call["context"]["events"]:
                self.assertLessEqual(observed["end"], cp)
                self.assertLessEqual(observed["available_at"], cp)
        for item in result["candidates"]:
            self.assertGreaterEqual(item["knowledge_cutoff"], item["event_available_at"])
            if item["event_id"] == "early":
                self.assertEqual(item["knowledge_cutoff"], 8)
            if item["event_id"] == "speech":
                self.assertGreaterEqual(item["knowledge_cutoff"], 18)
        for item in result["comments"]:
            self.assertGreaterEqual(item["time"] + 1e-9, item["knowledge_cutoff"] + .35)
            self.assertLess(item["time"], 30)
        self.assertEqual(result["diagnostics"]["event_filters"]["corrected_availability"], 1)
        self.assertEqual(result["diagnostics"]["near_end_events_skipped"], 1)

    def test_history_is_bounded_and_tail_window_is_flushed(self):
        config = settings()
        config["generation"].update(window_seconds=4, history_seconds=5)
        client = FakeClient()
        generate(analysis([event("one", 1), event("seven", 7), event("ten", 10),
                           event("fifteen", 15)], duration=20), config, client)
        for call in client.calls:
            context = call["context"]
            for observed in context["events"]:
                self.assertGreaterEqual(observed["available_at"], context["checkpoint"] - 5)
        sixteen = [c for c in client.calls if c["context"]["checkpoint"] == 16]
        self.assertTrue(sixteen)
        self.assertEqual([e["id"] for e in sixteen[0]["context"]["events"]], ["fifteen"])
        tail = generate(analysis([event("tail", 9)], duration=12), settings(), FakeClient())
        self.assertEqual({c["knowledge_cutoff"] for c in tail["candidates"]}, {9})

    def test_uncertain_observation_survives_context_and_provenance(self):
        client = FakeClient()
        result = generate(analysis([event("ambiguous", 1, "似乎有动物，种类看不清", uncertain=True)]),
                          settings(), client)
        for call in client.calls:
            self.assertTrue(call["context"]["events"][0]["uncertain"])
            self.assertIn("uncertain=true", call["system"])
        self.assertTrue(all(c["source_event"]["uncertain"] for c in result["candidates"]))

    def test_event_checkpoints_follow_speech_and_visual_availability(self):
        config = settings()
        config["generation"]["checkpoint_mode"] = "events"
        source = analysis([event("black", .3, salience=.1), event("snow", 6),
                           event("face", 12), event("speech", 12.18, end=14.36,
                                                        available_at=12.18, kind="speech"),
                           event("fight", 16.65), event("dark", 18, salience=0),
                           event("dragon", 26.025), event("reaction", 27.567),
                           event("hand", 30), event("end", 34.9)], duration=35)
        client = FakeClient()
        result = generate(source, config, client)
        self.assertEqual(result["diagnostics"]["checkpoints"],
                         [6, 12, 14.36, 16.65, 26.025, 27.567, 30])
        for item in result["candidates"]:
            self.assertEqual(item["knowledge_cutoff"], item["event_available_at"])
        for call in client.calls:
            cp = call["context"]["checkpoint"]
            self.assertTrue(all(e["available_at"] <= cp for e in call["context"]["events"]))
        for item in result["comments"]:
            self.assertGreaterEqual(item["time"] + 1e-9, item["knowledge_cutoff"] + .35)
        self.assertEqual(result["diagnostics"]["near_end_events_skipped"], 1)

    def test_event_merge_uses_latest_time_and_bounded_group_span(self):
        config = settings()
        config["generation"].update(checkpoint_mode="events", event_merge_seconds=1)
        source = analysis([event("one", 1), event("two", 1.8), event("future", 2.6)], duration=10)
        client = FakeClient()
        result = generate(source, config, client)
        self.assertEqual(result["diagnostics"]["checkpoints"], [1.8, 2.6])
        merged = [c for c in client.calls if c["context"]["checkpoint"] == 1.8]
        self.assertTrue(merged)
        for call in merged:
            self.assertEqual(call["context"]["target_event_ids"], ["one", "two"])
            self.assertNotIn('"future"', call["common"])
        self.assertEqual({c["knowledge_cutoff"] for c in result["candidates"] if c["event_id"] == "one"}, {1.8})
        config["generation"]["event_merge_seconds"] = 0
        unmerged = generate(source, config, FakeClient())
        self.assertEqual(unmerged["diagnostics"]["checkpoints"], [1, 1.8, 2.6])
        config["generation"].pop("checkpoint_mode")
        compatible = generate(source, config, FakeClient())
        self.assertEqual(compatible["diagnostics"]["checkpoint_mode"], "window")
        self.assertEqual(compatible["diagnostics"]["checkpoints"], [8])

    def test_quiet_events_remain_history_without_triggering_requests(self):
        config = settings()
        config["generation"].update(checkpoint_mode="events", min_trigger_salience=.2)
        source = analysis([event("black", .3, salience=.1), event("first", 3),
                           event("quiet", 4, salience=0), event("later", 7, salience=.2)], duration=10)
        client = FakeClient()
        result = generate(source, config, client)
        self.assertEqual(result["diagnostics"]["checkpoints"], [3, 7])
        self.assertEqual(result["diagnostics"]["low_salience_events_not_triggering"], 2)
        late = [c for c in client.calls if c["context"]["checkpoint"] == 7]
        self.assertEqual(late[0]["context"]["target_event_ids"], ["later"])
        self.assertEqual([e["id"] for e in late[0]["context"]["events"]], ["black", "first", "quiet", "later"])
        silent_client = FakeClient()
        generate(analysis([event("black", .3, salience=.1)]), config, silent_client)
        self.assertFalse(silent_client.calls)
        config["generation"]["min_trigger_salience"] = 0
        enabled = FakeClient()
        generate(source, config, enabled)
        target_ids = {eid for c in enabled.calls for eid in c["context"]["target_event_ids"]}
        self.assertTrue({"black", "quiet"} <= target_ids)

    def test_short_comment_default_and_invalid_checkpoint_configuration(self):
        client = FakeClient(reply=lambda c: {"comments": [
            {"event_id": "one", "text": "长" * 21}, {"event_id": "one", "text": "短" * 20}]})
        result = generate(analysis([event("one", 1)]), settings(), client)
        self.assertEqual(result["diagnostics"]["filtered"]["text_too_long"], 2)
        self.assertEqual([len(c["text"]) for c in result["comments"]], [20, 20])
        for key, value in [("checkpoint_mode", "future"), ("event_merge_seconds", -1),
                           ("min_trigger_salience", 1.01)]:
            with self.subTest(key=key):
                config = settings()
                config["generation"][key] = value
                client = FakeClient()
                with self.assertRaises(ValueError):
                    generate(analysis([event("one", 1)]), config, client)
                self.assertFalse(client.calls)

    def test_personas_models_rounds_are_independent_and_prefix_is_shared(self):
        config = settings()
        config["generation"]["rounds"] = 2
        client = FakeClient()
        result = generate(analysis([event("first", 1)], duration=12), config, client)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(len({c["common"] for c in client.calls}), 1)
        self.assertEqual(len({c["variant"] for c in client.calls}), 4)
        self.assertEqual(len({c["cache_key"] for c in client.calls}), 4)
        self.assertEqual({c["alias"] for c in client.calls}, {"a", "b"})
        self.assertEqual({c["round"] for c in result["candidates"]}, {1, 2})
        self.assertEqual(len({c["virtual_viewer_id"] for c in result["candidates"]}), 4)
        repeat = generate(analysis([event("first", 1)], duration=12), config, FakeClient())
        self.assertEqual(result, repeat)

    def test_silence_is_success_not_failure(self):
        result = generate(analysis([event("black", .3, "几乎全黑")]), settings(),
                          FakeClient(reply=lambda c: {"comments": []}))
        self.assertEqual(result["comments"], [])
        self.assertEqual(result["diagnostics"]["status"], "completed")
        self.assertEqual(result["diagnostics"]["calls_succeeded"], 2)

    def test_partial_analysis_propagates_even_when_generation_succeeds(self):
        source = analysis([event("first", 1)])
        source["diagnostics"] = {"status": "partial", "vision_failures": 1}
        result = generate(source, settings(), FakeClient())
        self.assertEqual(result["diagnostics"]["status"], "partial")
        self.assertEqual(result["diagnostics"]["analysis_status"], "partial")
        self.assertEqual(result["diagnostics"]["generation_status"], "completed")

    def test_zero_weights_and_zero_comment_limit_make_no_paid_calls(self):
        config = settings()
        for viewer in config["audience"]:
            viewer["weight"] = 0
        client = FakeClient()
        result = generate(analysis([event("first", 1)]), config, client)
        self.assertFalse(client.calls)
        self.assertEqual(result["diagnostics"]["weighted_calls_skipped"], 2)
        config = settings()
        config["generation"]["comments_per_window"] = 0
        generate(analysis([event("first", 1)]), config, client)
        self.assertFalse(client.calls)

    def test_partial_provider_failure_retains_other_viewer(self):
        result = generate(analysis([event("first", 1)]), settings(), FakeClient(fail={"b"}))
        self.assertEqual(result["diagnostics"]["status"], "partial")
        self.assertEqual(result["diagnostics"]["calls_failed"], 1)
        self.assertTrue(result["comments"])
        self.assertEqual({c["provider"] for c in result["comments"]}, {"a"})
        self.assertNotIn("private provider error", json.dumps(result))

    def test_all_failed_or_invalid_json_raise_without_mock(self):
        for client in (FakeClient(fail={"a", "b"}), FakeClient(reply=lambda c: "not JSON")):
            with self.subTest(client=client), self.assertRaises(GenerationError) as captured:
                generate(analysis([event("first", 1)]), settings(), client)
            self.assertEqual(captured.exception.diagnostics["calls_failed"], 2)
            self.assertEqual(captured.exception.diagnostics["calls_succeeded"], 0)
            self.assertEqual(captured.exception.diagnostics["status"], "failed")
            self.assertEqual({c["alias"] for c in client.calls}, {"a", "b"})

    def test_invalid_rows_are_reported_and_same_viewer_duplicates_removed(self):
        def reply(call):
            return {"comments": [None, {"event_id": "future", "text": "还没看过"},
                                 {"event_id": "first", "text": "https://example.com"},
                                 {"event_id": "first", "text": "长" * 100},
                                 {"event_id": "first", "text": "好\x00耶", "kind": ["malformed"]},
                                 {"event_id": "first", "text": "好耶"}]}
        result = generate(analysis([event("first", 1)]), settings(), FakeClient(reply=reply))
        self.assertEqual([c["text"] for c in result["comments"]], ["好耶", "好耶"])
        self.assertEqual({c["kind"] for c in result["comments"]}, {"reaction"})
        filtered = result["diagnostics"]["filtered"]
        for name in ("invalid_shape", "unknown_or_noncurrent_event", "url", "text_too_long",
                     "same_viewer_duplicate"):
            self.assertEqual(filtered[name], 2)


class SchedulingTests(unittest.TestCase):
    def test_exact_repeats_from_independent_viewers_are_kept_but_capped(self):
        candidates = [candidate(i, text="好耶", event_id="same") for i in range(10)]
        comments, diag = schedule_comments(candidates, 20, settings()["scheduler"])
        self.assertEqual(len(comments), 3)
        self.assertEqual(len({c["virtual_viewer_id"] for c in comments}), 3)
        self.assertEqual(diag["dropped"]["repetition_cap"], 7)
        self.assertEqual(text_key("好 耶"), text_key("好耶"))
        separate = candidates[:3] + [candidate(20, text="好耶", event_id="another")]
        self.assertEqual(len(schedule_comments(separate, 20, settings()["scheduler"])[0]), 4)

    def test_sliding_density_and_determinism_including_echoes(self):
        config = settings()["scheduler"]
        config.update(max_per_second=2, reaction_max=1.1, repeat_ratio=.5, max_delay=15)
        candidates = [candidate(i, cutoff=(i % 3) * .43) for i in range(15)]
        comments, diag = schedule_comments(candidates, 30, config, seed=7)
        self.assertGreater(len(comments), 10)
        times = [c["time"] for c in comments]
        for i in range(len(times) - 2):
            self.assertGreaterEqual(times[i + 2] - times[i], 1 - 1e-9)
        reordered = schedule_comments(list(reversed(candidates)), 30, config, seed=7)
        self.assertEqual((comments, diag), reordered)
        self.assertGreater(diag["delayed_for_density"], 0)

    def test_echoes_have_distinct_viewers_parent_provenance_and_valid_times(self):
        config = settings()["scheduler"]
        config.update(max_per_second=20, repeat_ratio=.5)
        comments, diag = schedule_comments([candidate(i) for i in range(8)], 20, config)
        self.assertEqual(diag["echoes_added"], 4)
        by_id = {c["id"]: c for c in comments}
        for echo in (c for c in comments if c["kind"] == "echo"):
            parent = by_id[echo["parent_id"]]
            self.assertEqual(echo["text"], parent["text"])
            self.assertNotEqual(echo["virtual_viewer_id"], parent["virtual_viewer_id"])
            self.assertGreaterEqual(echo["time"], parent["time"])
            self.assertLessEqual(echo["time"], parent["time"] + 2 + 1e-9)
            self.assertLessEqual(len(echo["text"]), 8)
            self.assertGreaterEqual(echo["time"], echo["knowledge_cutoff"] + .35)
            self.assertLess(echo["time"], 20)
            self.assertTrue(echo["synthetic"])
        counts = Counter((c["event_id"], text_key(c["text"])) for c in comments)
        self.assertLessEqual(max(counts.values()), 3)

    def test_echo_length_limit_does_not_remove_independent_long_repeats(self):
        config = settings()["scheduler"]
        config.update(max_per_second=20, repeat_ratio=1)
        phrase = "这个镜头看得我好紧张"
        candidates = [candidate(0, text=phrase, event_id="same"),
                      candidate(1, text=phrase, event_id="same"), candidate(2, text="哇")]
        before = json.dumps(candidates, ensure_ascii=False, sort_keys=True)
        comments, diag = schedule_comments(candidates, 20, config)
        originals = [c for c in comments if c["generation_method"] == "independent"]
        echoes = [c for c in comments if c["generation_method"] == "controlled_repeat"]
        self.assertEqual(len(originals), 3)
        self.assertEqual(sum(c["text"] == phrase for c in originals), 2)
        self.assertEqual([c["text"] for c in echoes], ["哇"])
        self.assertEqual(diag["echo_parents_too_long"], 2)
        self.assertEqual(diag["echo_max_chars"], 8)
        self.assertEqual(before, json.dumps(candidates, ensure_ascii=False, sort_keys=True))
        config["echo_max_chars"] = 20
        expanded, _ = schedule_comments(candidates, 20, config)
        self.assertEqual(originals, [c for c in expanded if c["generation_method"] == "independent"])
        self.assertTrue(any(c["text"] == phrase and c["kind"] == "echo" for c in expanded))

    def test_density_cannot_push_echo_past_parent_deadline(self):
        config = settings()["scheduler"]
        config.update(max_per_second=1, repeat_ratio=1)
        candidates = [candidate(0, text="哇")]
        candidates.extend(candidate(i, kind="detail") for i in range(1, 7))
        bounded, diag = schedule_comments(candidates, 20, config)
        self.assertEqual(diag["independent_comments"], 7)
        self.assertEqual(diag["echoes_added"], 0)
        self.assertEqual(diag["dropped"]["echo_no_room"], 1)
        self.assertEqual(diag["echo_max_parent_delay"], 2)
        config["echo_max_parent_delay"] = 8
        extended, _ = schedule_comments(candidates, 20, config)
        echo = next(c for c in extended if c["kind"] == "echo")
        parent = next(c for c in extended if c["id"] == echo["parent_id"])
        self.assertGreater(echo["time"], parent["time"] + 2)
        self.assertLessEqual(echo["time"], parent["time"] + 8)
        self.assertEqual(bounded, [c for c in extended if c["generation_method"] == "independent"])
        self.assertEqual(echo["knowledge_cutoff"], parent["knowledge_cutoff"])

    def test_echo_still_respects_original_max_delay_and_validates_options(self):
        config = settings()["scheduler"]
        config.update(max_per_second=1, repeat_ratio=1, max_delay=1.5, echo_max_parent_delay=20)
        comments, diag = schedule_comments([candidate(0, text="哇"), candidate(1, kind="detail")], 20, config)
        self.assertEqual(len(comments), 2)
        self.assertEqual(diag["dropped"]["echo_no_room"], 1)
        for key, value in [("echo_max_chars", 0), ("echo_max_chars", 1.5),
                           ("echo_max_parent_delay", -1), ("echo_max_parent_delay", float("inf"))]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                schedule_comments([], 20, {**settings()["scheduler"], key: value})

    def test_overflow_and_near_end_are_explicitly_dropped(self):
        config = settings()["scheduler"]
        config.update(max_delay=.4, max_per_second=1)
        candidates = [candidate(i) for i in range(5)]
        candidates += [candidate(10, cutoff=19.9), candidate(11, event_available_at=2)]
        comments, diag = schedule_comments(candidates, 20, config)
        self.assertEqual(len(comments), 1)
        self.assertEqual(diag["dropped"]["density_overflow"], 4)
        self.assertEqual(diag["dropped"]["no_time_remaining"], 1)
        self.assertEqual(diag["dropped"]["future_event"], 1)

    def test_target_rate_is_a_ceiling_spread_across_video(self):
        config = settings()["scheduler"]
        config["target_per_minute"] = 4
        comments, diag = schedule_comments([candidate(i, cutoff=i * 5) for i in range(12)], 60, config)
        self.assertEqual(len(comments), 4)
        self.assertLess(comments[0]["time"], 15)
        self.assertGreater(comments[-1]["time"], 45)
        self.assertEqual(diag["dropped"]["target_rate"], 8)
        config["target_per_minute"] = 0
        self.assertEqual(schedule_comments([candidate(0)], 60, config)[0], [])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_xml_is_parseable_escaped_and_identified_as_synthetic(self):
        path = self.directory / "comments.xml"
        text = '雪山<&>"与龙'
        info = write_xml([{**candidate(1, text=text), "time": 8.3501}], path)
        self.assertEqual(info["written"], 1)
        root = ET.parse(path).getroot()
        self.assertEqual(root.findtext("source"), "synthetic")
        node = root.find("d")
        self.assertEqual(node.text, text)
        self.assertEqual(len(node.attrib["p"].split(",")), 8)
        self.assertEqual(node.attrib["p"].split(",")[1], "1")
        self.assertIn("&lt;", path.read_text())
        self.assertIn("&amp;", path.read_text())

    def test_ass_injection_is_neutralized_and_lanes_are_distinct(self):
        path = self.directory / "comments.ass"
        payload = "{\\pos(0,0)}\\N\n弹幕<&>"
        comments = [{**candidate(i, text=payload if i == 0 else "另一条"),
                     "time": 1.355, "end_time": 10} for i in range(3)]
        info = write_ass(comments, path)
        data = path.read_text(encoding="utf-8-sig")
        lines = [line for line in data.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(info["written"], 3)
        self.assertEqual(len(lines), 3)
        self.assertNotIn("\\pos(", data)
        self.assertNotIn("\\N", data)
        self.assertIn("｛＼pos(0,0)｝＼N", data)
        self.assertTrue(all("0:00:01.36" in line for line in lines))
        y = [re.search(r"\\move\([^,]+,([^,]+),", line).group(1) for line in lines]
        self.assertEqual(len(set(y)), 3)
        self.assertEqual(escape_ass("{\\b1}hi\nthere"), "｛＼b1｝hi there")
        self.assertEqual(escape_html('<script>"&'), "&lt;script&gt;&quot;&amp;")

    def test_ass_lane_admission_prevents_overlap_and_reports_overflow(self):
        path = self.directory / "crowded.ass"
        comments = [{**candidate(i, text="很长很长很长的一条弹幕"), "time": .01,
                     "end_time": 4} for i in range(20)]
        info = write_ass(comments, path, width=320, height=180)
        self.assertLess(info["written"], 20)
        self.assertEqual(info["written"], info["lanes"])
        self.assertEqual(info["dropped"]["lane_overflow"], 20 - info["lanes"])
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("Dialogue:"):
                self.assertIn(",0:00:04.00,", line)

    def test_json_export_is_utf8_and_rejects_nonfinite_values(self):
        path = self.directory / "comments.json"
        write_json({"comments": [{"text": "好耶"}]}, path)
        self.assertIn("好耶", path.read_text())
        self.assertEqual(json.loads(path.read_text())["comments"][0]["text"], "好耶")
        with self.assertRaises(ValueError):
            write_json({"bad": float("nan")}, path)


if __name__ == "__main__":
    unittest.main()
