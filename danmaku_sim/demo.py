"""A labeled, hand-authored fixture for trying the UI without API credentials."""
from pathlib import Path
from .providers import write_json


def create_demo(directory, video=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "usage.jsonl").exists() or (directory / "comments.json").exists():
        raise ValueError("Demo output must be a fresh directory")
    texts = ["来啦", "这光影", "先看看", "刚刚那是什么", "等一下", "好家伙", "有点东西", "这个细节可以", "哈哈哈哈", "哈哈哈哈", "慢点慢点", "感觉不妙", "稳住", "啊这", "回去看一眼", "突然安静", "这个镜头好看", "还没看够"]
    duration = 60.0
    title = "离线示例 · 手工样本"
    if video:
        from .media import probe, file_hash
        video = Path(video).resolve()
        duration = probe(video)["duration"]
        write_json(directory / "media.json", {"path": str(video), "sha256": file_hash(video)})
    comments = [{"id": f"demo-{i}", "time": round((i + 1) * duration / (len(texts) + 2), 3), "text": text,
        "mode": 1, "color": 16777215, "size": 25, "persona": "offline-fixture", "provider": "none", "model": "none",
        "event_id": f"fixture-{i}", "kind": "fixture", "knowledge_cutoff": 0, "synthetic": True} for i, text in enumerate(texts)]
    return {"schema_version": 1, "video": {"title": title, "duration": duration}, "comments": comments, "candidates": [],
            "diagnostics": {"status": "demo", "note": "Hand-authored UI fixture. Not generated, not a quality evaluation, no model usage."}}
