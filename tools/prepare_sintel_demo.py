#!/usr/bin/env python3
"""Prepare the recorded Sintel sample; no model API calls or credentials needed.

Run from any directory with Python 3.11+. The default media and output paths
are relative to this repository, not to the shell's current directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sintel"
DOWNLOAD_URL = "https://media.w3.org/2010/05/sintel/trailer.mp4"
VIDEO_SHA256 = "b670602fa00934ca27c4351bb0efe7ea7a07fae57284e44226025eeed7c51254"
VIDEO_BYTES = 4_372_373
MEASURED_AT = "2026-11-06"
SAMPLE_ID = "sintel-recorded-2026-11-06"
MANIFEST_NAME = "recorded-sample.json"
PREPARED_BY = "tools/prepare_sintel_demo.py"
NOTICE = "真实生成样例回放，零新API调用，费用为历史记录"


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_video(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Video is not a regular file: {path}")
    if path.stat().st_size != VIDEO_BYTES or sha256(path) != VIDEO_SHA256:
        raise ValueError(f"Video does not match the recorded sample (size/SHA256): {path}")


def prepare_video(provided: Path | None) -> Path:
    if provided is not None:
        video = provided.expanduser().resolve()
        verify_video(video)
        return video

    video = ROOT / "runs" / "media" / "sintel-trailer.mp4"
    if video.exists():
        if not video.is_file():
            raise ValueError(f"Download destination is not a file: {video}")
        try:
            verify_video(video)
            print(f"复用已校验视频：{video}", flush=True)
            return video
        except ValueError:
            # Keep the existing file until a complete, verified replacement exists.
            print("已有默认视频校验不匹配，重新下载指定样例。", flush=True)

    video.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=video.name + ".", suffix=".part", dir=video.parent)
    part = Path(temporary)
    try:
        print(f"下载公开样例视频：{DOWNLOAD_URL}", flush=True)
        request = Request(DOWNLOAD_URL, headers={"User-Agent": "danmaku-simulator-recorded-demo/1"})
        total = 0
        with os.fdopen(descriptor, "wb") as destination, urlopen(request, timeout=30) as response:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > VIDEO_BYTES:
                    raise ValueError("Downloaded video exceeds the expected size")
                destination.write(chunk)
        verify_video(part)
        part.replace(video)
    finally:
        part.unlink(missing_ok=True)
    return video


def check_output(output: Path) -> None:
    """Only empty directories or a run explicitly owned by this sample are reusable."""
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f"Output is not a directory: {output}")
    if not any(output.iterdir()):
        return
    marker = output / MANIFEST_NAME
    if not marker.is_file():
        raise ValueError(f"Refusing to overwrite an existing ordinary run: {output}")
    try:
        old = read_json(marker)
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot verify existing recorded-sample identity: {output}") from error
    expected = {"schema_version": 1, "prepared_by": PREPARED_BY, "sample_id": SAMPLE_ID,
                "video_sha256": VIDEO_SHA256, "measured_at": MEASURED_AT,
                "usage_is_historical": True, "new_api_calls": 0}
    if any(old.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Output belongs to a different sample or ordinary run: {output}")
    # Refuse to discard subsequent model results placed into a former sample run.
    recorded_hashes = old.get("artifact_sha256")
    if not isinstance(recorded_hashes, dict):
        raise ValueError(f"Existing sample has no artifact ownership record: {output}")
    for name in ("comments.json", "analysis.json", "config.json", "usage.jsonl"):
        existing = output / name
        if existing.exists() and (not existing.is_file() or sha256(existing) != recorded_hashes.get(name)):
            raise ValueError(f"Recorded sample was modified; choose another --out directory: {existing}")


def prepare(output: Path, provided_video: Path | None = None) -> tuple[Path, int]:
    output = output.expanduser().resolve()
    check_output(output)
    source = read_json(SAMPLE / "SOURCE.json")
    result = read_json(SAMPLE / "comments.json")
    analysis = read_json(SAMPLE / "analysis.json")
    read_json(SAMPLE / "config.json")
    ledger = SAMPLE / "recorded-usage.jsonl"
    if not ledger.is_file():
        raise ValueError(f"Missing historical usage ledger: {ledger}")
    if (source.get("sha256") != VIDEO_SHA256 or source.get("bytes") != VIDEO_BYTES
            or source.get("measured_at") != MEASURED_AT):
        raise ValueError("Bundled sample SOURCE.json does not match this sample's identity")
    for document in (result, analysis):
        if document.get("video", {}).get("id") != VIDEO_SHA256:
            raise ValueError("Bundled comments/analysis belong to a different video")

    video = prepare_video(provided_video)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Source imports and bundled files are located by __file__, even outside the repo.
    sys.path.insert(0, str(ROOT))
    from danmaku_sim.cli import export_run

    with tempfile.TemporaryDirectory(prefix=".sintel-recorded-", dir=output.parent) as temporary:
        staging = Path(temporary)
        for name in ("analysis.json", "config.json", "comments.json"):
            shutil.copyfile(SAMPLE / name, staging / name)
        shutil.copyfile(ledger, staging / "usage.jsonl")
        write_json(staging / "media.json", {"path": str(video), "sha256": VIDEO_SHA256})
        # This only writes XML/ASS and summarizes the copied historical ledger.
        export_run(staging, result)
        export_report = staging / "export-report.json"
        if export_report.exists():
            report = read_json(export_report)
            if "path" in report:
                report["path"] = str(output / "export.ass")
                write_json(export_report, report)
        artifacts = {path.name: sha256(path) for path in staging.iterdir() if path.is_file()}
        manifest = {
            "schema_version": 1, "prepared_by": PREPARED_BY, "sample_id": SAMPLE_ID,
            "video_sha256": VIDEO_SHA256, "measured_at": MEASURED_AT,
            "usage_is_historical": True, "new_api_calls": 0, "notice": NOTICE,
            "source": source, "recorded_usage_source": "examples/sintel/recorded-usage.jsonl",
            "historical_usage_summary": read_json(staging / "usage-summary.json"),
            "artifact_sha256": artifacts,
        }
        write_json(staging / MANIFEST_NAME, manifest)
        check_output(output)
        output.mkdir(parents=True, exist_ok=True)
        # Replace only generated sample artifacts, preserving unrelated files.
        for path in staging.iterdir():
            if path.name != MANIFEST_NAME:
                path.replace(output / path.name)
        (staging / MANIFEST_NAME).replace(output / MANIFEST_NAME)
    return output, len(result["comments"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="使用已有同一视频；必须通过大小和 SHA256 校验")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "sintel-recorded",
                        help="样例运行目录，默认仓库内 runs/sintel-recorded")
    args = parser.parse_args(argv)
    try:
        output, count = prepare(args.out, args.video)
    except (OSError, ValueError, RuntimeError, KeyError, URLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(NOTICE)
    print(f"测量日期：{MEASURED_AT}；弹幕：{count} 条；输出：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
