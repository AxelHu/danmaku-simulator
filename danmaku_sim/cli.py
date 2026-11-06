from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

from .config import load_config
from .costs import load_usage
from .providers import ModelClient, ProviderError, canonical, write_json


def interaction_origin(analysis, result):
    """Bind an interaction-only ledger to the exact immutable source inputs."""
    return {"format": "danmaku-interaction-run/v1", "video_id": analysis["video"]["id"],
            "base_sha256": hashlib.sha256(canonical({"video": result["video"],
                                                     "comments": result["comments"]}).encode()).hexdigest(),
            "analysis_sha256": hashlib.sha256(canonical(analysis).encode()).hexdigest()}


def export_run(directory, result):
    from .exporters import write_xml, write_ass
    directory = Path(directory)
    write_json(directory / "comments.json", result)
    write_xml(result["comments"], directory / "export.xml")
    ass_stats = write_ass(result["comments"], directory / "export.ass")
    if ass_stats is not None:
        write_json(directory / "export-report.json", ass_stats)
    write_json(directory / "usage-summary.json", load_usage(directory))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="danmaku-sim", description="把视频理解为一群观众的反应，并记录实际模型用量。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "run"):
        command = commands.add_parser(name, help="分析视频" if name == "analyze" else "分析、生成并导出")
        command.add_argument("video", type=Path)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--transcript", type=Path, help="导入 JSON/SRT/VTT，跳过 STT 调用")
        command.add_argument("--no-stt", action="store_true")
        command.add_argument("--no-vision", action="store_true")
    command = commands.add_parser("generate", help="复用分析结果生成新观众反应")
    command.add_argument("--analysis", type=Path, required=True)
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command = commands.add_parser("interact", help="在已有独立弹幕上生成少量接话，单独记账")
    command.add_argument("--run", type=Path, required=True)
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command = commands.add_parser("publish", help="将选定运行发布为静态弹幕轨目录，不含视频")
    command.add_argument("run", type=Path)
    command.add_argument("--library", type=Path, required=True)
    command.add_argument("--track-id", default="default")
    command.add_argument("--label")
    command.add_argument("--language", default="zh-CN")
    command.add_argument("--offset", type=float, default=0, help="正值延迟弹幕，单位秒")
    command.add_argument("--scale", type=float, default=1)
    command.add_argument("--target-duration", type=float)
    command.add_argument("--bvid")
    command.add_argument("--page", type=int, default=1)
    command.add_argument("--cid")
    command.add_argument("--url", action="append", default=[], help="绑定的网页地址，可重复")
    command = commands.add_parser("match", help="按视频身份查询弹幕目录，不用标题自动匹配")
    command.add_argument("--library", type=Path, required=True)
    command.add_argument("--video-id")
    command.add_argument("--bvid")
    command.add_argument("--page", type=int)
    command.add_argument("--cid")
    command.add_argument("--url")
    command.add_argument("--duration", type=float)
    command = commands.add_parser("sidecar", help="按播放器规则导出视频同名 XML")
    command.add_argument("--track", type=Path, required=True)
    command.add_argument("--video", type=Path, required=True, help="目标视频路径或文件名")
    command.add_argument("--out-dir", type=Path, help="可先导出到单独目录，再部署到 NAS")
    command.add_argument("--replace", action="store_true", help="明确替换已有不同内容的同名 XML")
    command = commands.add_parser("export", help="重新导出 XML/ASS")
    command.add_argument("run", type=Path)
    command = commands.add_parser("costs", help="逐模型/阶段汇总用量")
    command.add_argument("run", type=Path)
    command = commands.add_parser("serve", help="启动只读预览")
    command.add_argument("--runs", type=Path, default=Path("runs"))
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8792)
    command.add_argument("--library", type=Path, help="仅对显式发布目录开放 /catalog 静态分发")
    command.add_argument("--catalog-only", action="store_true", help="只分发目录，关闭网页预览、运行与视频路由")
    command = commands.add_parser("demo", help="离线界面样本，不调用模型")
    command.add_argument("--out", type=Path, default=Path("runs/demo"))
    command.add_argument("--video", type=Path, help="可选：给样本绑定一个本地视频")
    command = commands.add_parser("plan", help="查看请求规模，不调用模型")
    command.add_argument("--config", type=Path, required=True)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--duration", type=float)
    source.add_argument("--analysis", type=Path, help="已有分析时给出事件请求上限")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.runs, args.host, args.port, library_dir=args.library, catalog_only=args.catalog_only)
            return 0
        if args.command == "publish":
            from .distribution import publish_run
            if args.cid and not args.bvid:
                raise ValueError("--cid requires --bvid")
            bindings = [{"site": "web", "url": url} for url in args.url]
            if args.bvid:
                binding = {"site": "bilibili", "bvid": args.bvid, "page": args.page}
                if args.cid:
                    binding["cid"] = args.cid
                bindings.append(binding)
            timeline = {"scale": args.scale, "offset": args.offset}
            if args.target_duration is not None:
                timeline["target_duration"] = args.target_duration
            result = publish_run(args.run, args.library, track_id=args.track_id, label=args.label,
                                 language=args.language, timeline=timeline, bindings=bindings)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "match":
            from .distribution import load_catalog, match_tracks
            result = match_tracks(load_catalog(args.library), video_id=args.video_id, bvid=args.bvid,
                                  page=args.page, cid=args.cid, url=args.url, duration=args.duration)
            print(json.dumps({"matches": result}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "sidecar":
            from .distribution import write_sidecar
            print(json.dumps(write_sidecar(args.track, args.video, out_dir=args.out_dir, replace=args.replace), ensure_ascii=False, indent=2))
            return 0
        if args.command == "costs":
            print(json.dumps(load_usage(args.run), ensure_ascii=False, indent=2))
            return 0
        if args.command == "export":
            result = json.loads((args.run / "comments.json").read_text(encoding="utf-8"))
            export_run(args.run, result)
            print(f"Exported {len(result['comments'])} comments")
            return 0
        if args.command == "demo":
            from .demo import create_demo
            result = create_demo(args.out, args.video)
            export_run(args.out, result)
            print(f"Offline fixture ready: {args.out}; {len(result['comments'])} comments. No model calls.")
            return 0
        config = load_config(args.config)
        if args.command == "interact":
            from .interactions import add_interactions
            if args.run.resolve() == args.out.resolve():
                raise ValueError("Use a different output directory to retain the original run and its ledger")
            result = json.loads((args.run / "comments.json").read_text(encoding="utf-8"))
            analysis = json.loads((args.run / "analysis.json").read_text(encoding="utf-8"))
            if (not isinstance(analysis, dict) or not isinstance(analysis.get("video"), dict)
                    or not isinstance(result, dict) or not isinstance(result.get("video"), dict)
                    or not isinstance(result.get("comments"), list)):
                raise ValueError("The source run needs video, comments and analysis objects")
            identity = analysis.get("video", {}).get("id")
            if not identity or result.get("video", {}).get("id") != identity:
                raise ValueError("The source run and analysis need the same video identity")
            origin = interaction_origin(analysis, result)
            origin_path = args.out / "interaction-run.json"
            if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
                if not origin_path.is_file() or json.loads(origin_path.read_text(encoding="utf-8")) != origin:
                    raise ValueError("Use a new/empty output directory, or resume an interaction run with the same source inputs")
            for name in ("analysis.json", "comments.json", "media.json"):
                existing_path = args.out / name
                if existing_path.exists():
                    old = json.loads(existing_path.read_text(encoding="utf-8"))
                    if (old.get("sha256") if name == "media.json" else old.get("video", {}).get("id")) != identity:
                        raise ValueError("Output belongs to another video")
            manifest_path = args.run / "media.json"
            manifest = None
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("sha256") != identity:
                    raise ValueError("Source media binding does not match analysis")
            args.out.mkdir(parents=True, exist_ok=True)
            write_json(origin_path, origin)
            if manifest is not None:
                write_json(args.out / "media.json", manifest)
            config.setdefault("interactions", {})["enabled"] = True
            write_json(args.out / "analysis.json", analysis)
            write_json(args.out / "config.json", config)
            client = ModelClient(config, args.out)
            result = add_interactions(analysis, config, client, result)
            result.setdefault("diagnostics", {})["base_comments_reused"] = True
            export_run(args.out, result)
            print(json.dumps({"comments": len(result["comments"]), "diagnostics": result["diagnostics"], "usage": load_usage(args.out)["summary"]}, ensure_ascii=False, indent=2))
            return 2 if result["diagnostics"].get("status") == "partial" else 0
        if args.command == "plan":
            import math
            plan_analysis = json.loads(args.analysis.read_text(encoding="utf-8")) if args.analysis else None
            duration = float(plan_analysis["video"]["duration"]) if plan_analysis else args.duration
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("duration must be positive")
            generation = config["generation"]
            mode = generation.get("checkpoint_mode", "window")
            multiplier = len(config["audience"]) * generation.get("rounds", 1)
            windows = math.ceil(duration / generation.get("window_seconds", 8))
            if mode == "events":
                if plan_analysis:
                    from .engine import _normalize_events
                    events, _ = _normalize_events(plan_analysis, duration)
                    checkpoints = sum(event["salience"] >= generation.get("min_trigger_salience", .2) for event in events)
                    requests = checkpoints * multiplier
                else:
                    requests = None
                note = "Event-mode request count depends on the analyzed events. Supply --analysis for an upper bound; merging and participation may reduce it."
            elif mode == "window":
                requests = windows * multiplier
                note = "Upper bound before empty-window and participation filtering."
            else:
                raise ValueError("checkpoint_mode must be events or window")
            print(json.dumps({"duration": duration, "checkpoint_mode": mode, "generation_request_upper_bound": requests,
                "fixed_window_reference_requests": windows * multiplier,
                "vision_request_upper_bound": config["analysis"].get("max_frames", 24),
                "stt_chunks": math.ceil(duration / config.get("stt", {}).get("chunk_seconds", 120)),
                "note": "No API calls made. " + note}, ensure_ascii=False, indent=2))
            return 0
        args.out.mkdir(parents=True, exist_ok=True)
        client = ModelClient(config, args.out)
        if args.command in ("analyze", "run"):
            from .media import analyze
            analysis = analyze(args.video, args.out, config, client, transcript_path=args.transcript, no_stt=args.no_stt, no_vision=args.no_vision)
            write_json(args.out / "config.json", config)
            print(f"Analysis: {len(analysis['transcript'])} speech segments, {analysis['diagnostics']['frames_analyzed']} visual observations", flush=True)
            if args.command == "analyze":
                write_json(args.out / "usage-summary.json", load_usage(args.out))
                return 0 if analysis["diagnostics"]["status"] == "completed" else 2
        else:
            analysis = json.loads(args.analysis.read_text(encoding="utf-8"))
            if not isinstance(analysis, dict) or not isinstance(analysis.get("video"), dict) or not analysis["video"].get("id"):
                raise ValueError("Analysis must have a video identity")
            output_analysis = args.out / "analysis.json"
            if output_analysis.exists():
                existing = json.loads(output_analysis.read_text(encoding="utf-8"))
                if existing.get("video", {}).get("id") != analysis["video"]["id"]:
                    raise ValueError("Output run belongs to a different video; choose a new directory")
            output_manifest = args.out / "media.json"
            if output_manifest.exists() and json.loads(output_manifest.read_text()).get("sha256") != analysis["video"]["id"]:
                raise ValueError("Output run has a different media binding; choose a new directory")
            source_manifest = args.analysis.parent / "media.json"
            if source_manifest.exists():
                media = json.loads(source_manifest.read_text(encoding="utf-8"))
                if media.get("sha256") != analysis["video"]["id"]:
                    raise ValueError("Source media binding does not match analysis")
                if source_manifest.resolve() != output_manifest.resolve():
                    write_json(output_manifest, media)
            write_json(output_analysis, analysis)
            # Configuration contains environment variable NAMES, never keys.
            write_json(args.out / "config.json", config)
        from .engine import generate, GenerationError
        try:
            result = generate(analysis, config, client)
        except GenerationError as error:
            write_json(args.out / "generation-failure.json", error.diagnostics)
            write_json(args.out / "usage-summary.json", load_usage(args.out))
            raise
        export_run(args.out, result)
        print(json.dumps({"comments": len(result["comments"]), "diagnostics": result["diagnostics"], "usage": load_usage(args.out)["summary"]}, ensure_ascii=False, indent=2))
        return 2 if result["diagnostics"].get("status") == "partial" else 0
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
