"""An interaction output must never absorb an existing generation ledger."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from danmaku_sim.cli import main


class InteractionOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.out = self.root / "interaction"
        self.video = {"id": "same-video", "duration": 10}
        self.base = {"video": self.video, "comments": [{"id": "a", "time": 1, "text": "小鸟吗"}],
                     "diagnostics": {"status": "completed"}}
        (self.source / "analysis.json").write_text(json.dumps({"video": self.video, "events": []}))
        (self.source / "comments.json").write_text(json.dumps(self.base))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"providers": {"p": {"type": "openai",
            "base_url": "https://api.example.test/v1", "model": "test", "api_key_env": "TEST_ONLY_KEY"}},
            "audience": [{"id": "viewer", "provider": "p"}]}))

    def run_cli(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(["interact", "--run", str(self.source), "--config", str(self.config),
                         "--out", str(self.out)])

    def snapshot(self):
        return {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob("*") if p.is_file()}

    def test_existing_same_video_run_and_ledger_remain_untouched(self):
        self.out.mkdir()
        (self.out / "analysis.json").write_bytes((self.source / "analysis.json").read_bytes())
        (self.out / "comments.json").write_bytes((self.source / "comments.json").read_bytes())
        (self.out / "usage.jsonl").write_text('{"stage":"generation","actual_request":true}\n')
        before = self.snapshot()
        with patch("danmaku_sim.cli.ModelClient") as client:
            self.assertEqual(self.run_cli(), 1)
            client.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_same_source_resume_is_allowed_but_changed_source_is_rejected(self):
        with patch("danmaku_sim.cli.ModelClient"), patch("danmaku_sim.interactions.add_interactions",
                side_effect=lambda analysis, config, client, result: deepcopy(result)) as interact:
            self.assertEqual(self.run_cli(), 0)
            marker = json.loads((self.out / "interaction-run.json").read_text())
            self.assertEqual(marker["video_id"], self.video["id"])
            self.assertEqual(len(marker["base_sha256"]), 64)
            self.assertEqual(self.run_cli(), 0)
            self.assertEqual(interact.call_count, 2)
            before = self.snapshot()
            changed = deepcopy(self.base)
            changed["comments"][0]["text"] = "另一套观众"
            (self.source / "comments.json").write_text(json.dumps(changed))
            self.assertEqual(self.run_cli(), 1)
            self.assertEqual(interact.call_count, 2)
            self.assertEqual(self.snapshot(), before)
            (self.source / "comments.json").write_text(json.dumps(self.base))
            (self.source / "analysis.json").write_text(json.dumps({"video": self.video, "events": [{"id": "new"}]}))
            self.assertEqual(self.run_cli(), 1)
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
