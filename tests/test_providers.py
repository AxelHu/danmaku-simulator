"""Regression tests for billing, request identity and provider boundaries.

All provider calls are mocked; these tests cannot incur API charges.
"""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from danmaku_sim.config import load_config
from danmaku_sim.costs import load_usage, summarize
from danmaku_sim.providers import ModelClient, ProviderError, normalize_usage, price_usage


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "providers": {"test": {
                "type": "openai_chat", "base_url": "https://models.invalid/v1",
                "model": "test-model", "api_key_env": "DANMAKU_UNIT_TEST_KEY",
                "pricing": {
                    "input_per_million": 2, "cached_input_per_million": .2,
                    "cache_write_per_million": 2.5, "output_per_million": 10,
                    "currency": "USD", "billing_mode": "payg",
                },
            }},
            "audience": [{"id": "viewer", "provider": "test"}],
        }
        environment = patch.dict(os.environ, {"DANMAKU_UNIT_TEST_KEY": "unit-test-only"})
        environment.start()
        self.addCleanup(environment.stop)

    def client(self, name="run"):
        return ModelClient(self.config, self.root / name, self.root / "cache")

    @staticmethod
    def response(text="test response"):
        return {
            "id": "test-request", "model": "test-model",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30},
                "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }

    @staticmethod
    def opener(document):
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(document).encode())
        return opener

    def test_cache_tokens_and_reasoning_are_not_double_counted(self):
        pricing = self.config["providers"]["test"]["pricing"]
        openai = normalize_usage(self.response()["usage"], "openai_chat")
        self.assertEqual((openai["input_tokens"], openai["fresh_input_tokens"],
                          openai["cached_input_tokens"], openai["output_tokens"]), (100, 70, 30, 20))
        self.assertEqual(openai["reasoning_tokens"], 5)
        self.assertAlmostEqual(price_usage(openai, pricing), .000346)
        anthropic = normalize_usage({
            "input_tokens": 70, "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40, "output_tokens": 20,
        }, "anthropic")
        self.assertEqual(anthropic["input_tokens"], 140)
        self.assertAlmostEqual(price_usage(anthropic, pricing), .000446)

    def test_missing_or_negative_usage_is_not_a_known_zero_bill(self):
        cases = [
            ("openai_chat", {}),
            ("openai_chat", {"prompt_tokens": -1, "completion_tokens": 0}),
            ("anthropic", {"input_tokens": -1, "output_tokens": 0}),
        ]
        for protocol, raw in cases:
            with self.subTest(protocol=protocol, raw=raw):
                usage = normalize_usage(raw, protocol)
                self.assertFalse(usage["usage_known"])
                self.assertIsNone(price_usage(usage, self.config["providers"]["test"]["pricing"]))

    def test_invalid_cache_breakdown_is_unpriced(self):
        for details in ("invalid", {"cached_tokens": -1}, {"cached_tokens": 101}):
            with self.subTest(details=details):
                raw = {"prompt_tokens": 100, "completion_tokens": 20,
                       "prompt_tokens_details": details}
                usage = normalize_usage(raw, "openai_chat")
                self.assertIsNone(price_usage(usage, self.config["providers"]["test"]["pricing"]))

    def test_malformed_billed_responses_always_leave_a_ledger(self):
        documents = [
            [],
            {"choices": [None], "usage": self.response()["usage"]},
        ]
        for number, document in enumerate(documents):
            with self.subTest(document=document):
                client = self.client("malformed-" + str(number))
                with patch("danmaku_sim.providers.urllib.request.build_opener",
                           return_value=self.opener(document)):
                    with self.assertRaises(ProviderError):
                        client.complete("test", "system", "context", "viewer")
                report = load_usage(client.run_dir)["summary"]
                self.assertEqual(report["request_count"], 1)
                self.assertEqual(report["failed_requests"], 1)
                ledger = (client.run_dir / "usage.jsonl").read_text()
                self.assertNotIn("unit-test-only", ledger)
                if number == 0:
                    self.assertFalse(report["cost_complete"])
                else:
                    self.assertAlmostEqual(report["estimated_cost"], .000346)

    def test_result_reuse_is_free_but_new_viewer_or_model_is_a_request(self):
        client = self.client()
        opener = self.opener(self.response())
        with patch("danmaku_sim.providers.urllib.request.build_opener", return_value=opener):
            first = client.complete("test", "system", "context", "viewer-a")
            reused = client.complete("test", "system", "context", "viewer-a")
            client.complete("test", "system", "context", "viewer-b")
            self.config["providers"]["test"]["model"] = "another-model"
            client.complete("test", "system", "context", "viewer-b")
        self.assertFalse(first["local_cache_hit"])
        self.assertTrue(reused["local_cache_hit"])
        self.assertEqual(opener.open.call_count, 3)
        summary = load_usage(client.run_dir)["summary"]
        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["local_cache_hits"], 1)
        self.assertEqual(summary["input_tokens"], 300)
        self.assertEqual(summary["cached_input_tokens"], 90)

    def test_bad_json_explicit_rerun_requests_again_without_erasing_usage(self):
        from danmaku_sim.engine import GenerationError, generate

        self.config["generation"] = {"checkpoint_mode": "events", "concurrency": 1}
        analysis = {
            "video": {"id": "test-video", "duration": 10},
            "events": [{"id": "s0", "start": 1, "end": 2, "available_at": 2,
                        "kind": "speech", "text": "这是一段测试对白", "salience": .6}],
        }
        documents = iter([
            self.response("not valid JSON"),
            self.response(json.dumps({"comments": [
                {"event_id": "s0", "text": "这句来得突然", "kind": "reaction"}
            ]})),
        ])
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(next(documents)).encode())
        client = self.client()
        with patch("danmaku_sim.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(GenerationError):
                generate(analysis, self.config, client)
            self.assertEqual(opener.open.call_count, 1, "Invalidation must not silently retry")
            first_ledger = (client.run_dir / "usage.jsonl").read_text().splitlines()
            self.assertEqual(len(first_ledger), 1)
            self.assertTrue(list((client.run_dir / "responses").glob("*.json")))
            self.assertFalse(list(client.cache_dir.glob("*.json")))

            result = generate(analysis, self.config, client)

        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(result["diagnostics"]["status"], "completed")
        self.assertEqual(len(result["comments"]), 1)
        final_ledger = (client.run_dir / "usage.jsonl").read_text().splitlines()
        self.assertEqual(len(final_ledger), 2)
        self.assertEqual(final_ledger[0], first_ledger[0])
        summary = load_usage(client.run_dir)["summary"]
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["local_cache_hits"], 0)
        self.assertEqual(summary["input_tokens"], 200)
        self.assertAlmostEqual(summary["estimated_cost"], .000692)

    def test_frame_bytes_are_part_of_result_cache_identity(self):
        image = self.root / "frame.jpg"
        image.write_bytes(b"first image")
        client = self.client()
        opener = self.opener(self.response())
        with patch("danmaku_sim.providers.urllib.request.build_opener", return_value=opener):
            client.complete("test", "system", "context", "viewer",
                            images=[{"path": str(image), "time": 1, "sha256": "stale"}])
            image.write_bytes(b"changed image")
            result = client.complete("test", "system", "context", "viewer",
                                     images=[{"path": str(image), "time": 1, "sha256": "stale"}])
        self.assertFalse(result["local_cache_hit"])
        self.assertEqual(opener.open.call_count, 2)

    def test_stt_uses_the_same_endpoint_and_secret_rules(self):
        for update in (
            {"base_url": "http://remote.invalid"},
            {"base_url": "https://models.invalid?key=secret"},
            {"api_key": "unit-test-only"},
        ):
            with self.subTest(update=update):
                config = copy.deepcopy(self.config)
                config["stt"] = {"type": "minimax", "base_url": "https://models.invalid",
                                 "api_key_env": "DANMAKU_UNIT_TEST_KEY", **update}
                path = self.root / "config.json"
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_invalid_prices_are_rejected_before_requests(self):
        for rate in (-1, float("nan"), float("inf")):
            with self.subTest(rate=rate):
                config = copy.deepcopy(self.config)
                config["providers"]["test"]["pricing"]["input_per_million"] = rate
                path = self.root / "config.json"
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_unknown_cost_stays_incomplete_and_mixed_currency_is_not_summed(self):
        report = summarize([{"status": "error", "request_count": 1, "estimated_cost": None}])
        self.assertFalse(report["cost_complete"])
        self.assertEqual(report["unpriced_requests"], 1)
        self.assertIsNone(report["actual_cost"])
        with self.assertRaises(ValueError):
            summarize([{"currency": "USD"}, {"currency": "CNY"}])


if __name__ == "__main__":
    unittest.main()
