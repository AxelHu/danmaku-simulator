"""Portable, versioned comment tracks and a static catalog, without source media.

The same directory can be copied to a disk/NAS or hosted by HTTP/object storage.
Only an explicit publish operation adds a run to this library.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import __version__
from .exporters import write_ass, write_xml
from .providers import canonical, write_json


TRACK_FORMAT = "danmaku-track/v1"
CATALOG_FORMAT = "danmaku-catalog/v1"
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
BVID = re.compile(r"BV[A-Za-z0-9]{10}\Z")
COMMENT_FIELDS = ("id", "time", "text", "mode", "color", "size", "persona", "kind",
                  "virtual_viewer_id", "generation_method", "parent_id", "reply_to",
                  "interaction_type", "parent_text", "parent_time", "depth", "synthetic")


def number(value, name, *, minimum=None, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if (positive and value <= 0) or (minimum is not None and value < minimum):
        raise ValueError(f"Invalid {name}")
    return float(value)


def canonical_url(value):
    if not isinstance(value, str):
        raise ValueError("A web binding needs a URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Web bindings require an HTTP(S) URL without credentials")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Control characters are not valid in a URL")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        host += f":{port}"
    original_query = parse_qsl(parsed.query, keep_blank_values=True)
    secret_fields = {"access_token", "token", "authorization", "signature", "sig", "x-amz-signature",
                     "x-amz-credential", "x-amz-security-token", "q-signature", "q-ak"}
    fragment_query = parse_qsl(parsed.fragment.split("?", 1)[-1], keep_blank_values=True)
    if any(key.lower() in secret_fields for key, _ in original_query + fragment_query):
        raise ValueError("Use a stable page URL; credential/signed query parameters cannot be published in bindings")
    query = [(key, item) for key, item in original_query
             if not key.lower().startswith("utm_") and key not in ("spm_id_from", "share_source")]
    # Fragments may identify SPA videos (/#/watch/1); do not merge those editions.
    return urlunsplit((parsed.scheme, host, parsed.path or "/", urlencode(sorted(query)), parsed.fragment))


def normalize_bindings(bindings):
    if not isinstance(bindings, list):
        raise ValueError("bindings must be an array")
    result = []
    for item in bindings:
        if not isinstance(item, dict):
            raise ValueError("A binding must be an object")
        if item.get("site") == "bilibili":
            if not isinstance(item.get("bvid"), str) or not BVID.fullmatch(item["bvid"]):
                raise ValueError("Invalid Bilibili BVID")
            page = item.get("page", 1)
            if type(page) is not int or page < 1:
                raise ValueError("Bilibili page must be a positive integer")
            binding = {"site": "bilibili", "bvid": item["bvid"], "page": page}
            if item.get("cid") is not None:
                cid = str(item["cid"])
                if not cid.isdecimal() or int(cid) < 1:
                    raise ValueError("Bilibili cid must be a positive decimal identifier")
                binding["cid"] = cid
        elif item.get("site") == "web":
            binding = {"site": "web", "url": canonical_url(item.get("url"))}
        else:
            raise ValueError("Binding site must be bilibili or web")
        if binding not in result:
            result.append(binding)
    return sorted(result, key=canonical)


def normalize_timeline(timeline=None):
    timeline = {} if timeline is None else timeline
    if not isinstance(timeline, dict):
        raise ValueError("timeline must be an object")
    result = {"scale": number(timeline.get("scale", 1), "scale", positive=True),
              "offset": number(timeline.get("offset", 0), "offset")}
    if timeline.get("target_duration") is not None:
        result["target_duration"] = number(timeline["target_duration"], "target_duration", positive=True)
    return result


def make_track(result, *, track_id="default", label=None, language="zh-CN", timeline=None, bindings=None):
    if not isinstance(track_id, str) or not SLUG.fullmatch(track_id):
        raise ValueError("track_id must be a short alphanumeric/dash/underscore slug")
    if not isinstance(result, dict) or not isinstance(result.get("video"), dict) or not isinstance(result.get("comments"), list):
        raise ValueError("A run needs video and comments")
    source = result["video"]
    if not isinstance(source.get("id"), str) or not source["id"]:
        raise ValueError("A source video identity is required")
    video = {"id": source["id"], "title": str(source.get("title", "Untitled")),
             "duration": number(source.get("duration"), "video.duration", positive=True)}
    for field in ("width", "height"):
        if type(source.get(field)) is int and source[field] > 0:
            video[field] = source[field]
    comments, seen = [], set()
    for index, original in enumerate(result["comments"]):
        if not isinstance(original, dict) or not isinstance(original.get("text"), str) or not original["text"].strip():
            raise ValueError(f"Invalid comment {index}")
        moment = number(original.get("time"), "comment.time", minimum=0)
        if moment >= video["duration"]:
            raise ValueError(f"Comment {index} falls outside the source timeline")
        comment = {field: original[field] for field in COMMENT_FIELDS if field in original}
        comment["id"] = str(comment.get("id", f"comment-{index}"))
        if comment["id"] in seen:
            raise ValueError("Duplicate comment id")
        seen.add(comment["id"])
        comment.update(time=moment, synthetic=True)
        for field, default, low, high in (("mode", 1, 1, 6), ("color", 16777215, 0, 16777215), ("size", 25, 8, 96)):
            value = comment.get(field, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid comment {field}")
            comment[field] = value
        if "parent_time" in comment:
            comment["parent_time"] = number(comment["parent_time"], "parent_time", minimum=0)
        if "depth" in comment and (type(comment["depth"]) is not int or comment["depth"] != 1):
            raise ValueError("Only one-level interaction threads are supported")
        for field in set(COMMENT_FIELDS) - {"time", "mode", "color", "size", "parent_time", "depth", "synthetic"}:
            if field in comment and not isinstance(comment[field], str):
                raise ValueError(f"Comment {field} must be a string")
        comments.append(comment)
    comments.sort(key=lambda row: (row["time"], row["id"]))
    track = {"format": TRACK_FORMAT, "schema_version": 1, "video": video,
             "timeline": normalize_timeline(timeline), "bindings": normalize_bindings([] if bindings is None else bindings),
             "metadata": {"track_id": track_id, "label": str(label or track_id), "language": str(language),
                          "synthetic": True, "generator": f"danmaku-simulator/{__version__}",
                          "comment_count": len(comments)}, "comments": comments}
    track["metadata"]["revision"] = hashlib.sha256(canonical(track).encode()).hexdigest()
    return track


def aligned_comments(track):
    timeline = normalize_timeline(track.get("timeline"))
    duration = timeline.get("target_duration", track["video"]["duration"] * timeline["scale"] + timeline["offset"])
    number(duration, "aligned duration", positive=True)
    result, dropped = [], 0
    for comment in track["comments"]:
        moment = round(comment["time"] * timeline["scale"] + timeline["offset"], 3)
        if moment < 0 or moment >= duration:
            dropped += 1
            continue
        result.append({**comment, "time": moment, "end_time": duration})
    return result, {"comment_count": len(result), "dropped_outside_timeline": dropped, "duration": duration}


def load_catalog(directory):
    path = Path(directory) / "catalog.json"
    if not path.exists():
        return {"format": CATALOG_FORMAT, "schema_version": 1, "items": []}
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("format") != CATALOG_FORMAT or not isinstance(catalog.get("items"), list):
        raise ValueError("Unsupported or malformed catalog")
    seen = set()
    for item in catalog["items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("media_key"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["media_key"])
                or not isinstance(item.get("video"), dict) or not isinstance(item["video"].get("id"), str)
                or not isinstance(item.get("tracks"), list) or item["media_key"] in seen):
            raise ValueError("Invalid or duplicate catalog media entry")
        seen.add(item["media_key"])
        number(item["video"].get("duration"), "catalog video.duration", positive=True)
        track_ids = set()
        for track in item["tracks"]:
            if (not isinstance(track, dict) or not isinstance(track.get("id"), str)
                    or not SLUG.fullmatch(track["id"]) or track["id"] in track_ids):
                raise ValueError("Invalid or duplicate catalog track")
            track_ids.add(track["id"])
            normalize_timeline(track.get("timeline"))
            normalize_bindings(track.get("bindings", []))
    return catalog


@contextmanager
def catalog_lock(directory):
    lock = directory / ".catalog.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("A publisher holds .catalog.lock; finish it before publishing again") from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def publish_run(run_dir, library_dir, **options):
    result = json.loads((Path(run_dir) / "comments.json").read_text(encoding="utf-8"))
    track = make_track(result, **options)
    aligned, alignment = aligned_comments(track)
    directory = Path(library_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = track["metadata"]
    media_key = track["video"]["id"] if re.fullmatch(r"[0-9a-f]{64}", track["video"]["id"]) else hashlib.sha256(track["video"]["id"].encode()).hexdigest()
    relative = Path("tracks") / media_key / metadata["track_id"] / metadata["revision"]
    target = directory / relative
    with catalog_lock(directory):
        catalog = load_catalog(directory)
        for parent in [directory / "tracks", directory / "tracks" / media_key, target.parent, target]:
            if parent.is_symlink():
                raise ValueError("Published track paths cannot be symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".publishing-", dir=target.parent) as temporary:
            staging = Path(temporary)
            write_json(staging / "comments.json", track)
            write_xml(aligned, staging / "comments.xml")
            ass = write_ass(aligned, staging / "comments.ass")
            formats = {}
            for extension in ("json", "xml", "ass"):
                path = staging / f"comments.{extension}"
                payload = path.read_bytes()
                formats[extension] = {"href": (relative / path.name).as_posix(), "sha256": hashlib.sha256(payload).hexdigest(),
                                      "bytes": len(payload), "comment_count": len(track["comments"]) if extension == "json" else alignment["comment_count"] if extension == "xml" else ass["written"]}
            reused = target.exists()
            if reused:
                if not target.is_dir() or any(not (target / path.name).is_file() or (target / path.name).is_symlink()
                    or (target / path.name).read_bytes() != path.read_bytes() for path in staging.iterdir()):
                    raise ValueError("An immutable revision differs from its expected content")
            else:
                staging.rename(target)
            entry = {"id": metadata["track_id"], "label": metadata["label"], "revision": metadata["revision"],
                     "language": metadata["language"], "synthetic": True, "comment_count": len(track["comments"]),
                     "bindings": track["bindings"], "timeline": track["timeline"], "formats": formats,
                     "alignment": alignment, "ass_dropped": ass["dropped"]}
            item = next((item for item in catalog["items"] if item.get("media_key") == media_key), None)
            if item is None:
                item = {"media_key": media_key, "video": track["video"], "tracks": []}
                catalog["items"].append(item)
            elif item.get("video", {}).get("id") != track["video"]["id"]:
                raise ValueError("Media identity collision")
            item["tracks"] = sorted([row for row in item["tracks"] if row["id"] != metadata["track_id"]] + [entry], key=lambda row: row["id"])
            catalog["items"].sort(key=lambda item: item["media_key"])
            write_json(directory / "catalog.json", catalog)
    return {"library": str(directory), "media_key": media_key, "track": entry, "reused_revision": reused}


def match_tracks(catalog, *, video_id=None, bvid=None, page=None, cid=None, url=None, duration=None):
    """Exact identities identify candidates; duration is a separate alignment gate."""
    if duration is not None:
        duration = number(duration, "duration", positive=True)
    if page is not None and (type(page) is not int or page < 1):
        raise ValueError("page must be a positive integer")
    query_url = canonical_url(url) if url else None
    found = []
    for item in catalog["items"]:
        for track in item["tracks"]:
            reasons, exact = [], False
            if video_id and item["video"]["id"] == video_id:
                reasons.append("video_id")
                exact = True
            for binding in track.get("bindings", []):
                if binding.get("site") == "bilibili" and bvid and binding.get("bvid") == bvid:
                    if page is not None and binding["page"] != page:
                        continue
                    if cid is not None and binding.get("cid") is not None and str(binding["cid"]) != str(cid):
                        continue
                    reasons.append("bvid")
                    if page is not None:
                        reasons.append("page")
                        exact = True
                    if cid is not None and str(binding.get("cid")) == str(cid):
                        reasons.append("cid")
                        exact = True
                elif binding.get("site") == "web" and query_url and binding.get("url") == query_url:
                    reasons.append("url")
                    exact = True
            if not reasons:
                continue
            timeline = normalize_timeline(track.get("timeline"))
            expected = timeline.get("target_duration", item["video"]["duration"] * timeline["scale"] + timeline["offset"])
            difference = None if duration is None else duration - expected
            aligned = difference is None or abs(difference) <= max(2.0, expected * .01)
            found.append({"media_key": item["media_key"], "video": item["video"], "track": track,
                          "match": {"matched_on": sorted(set(reasons)), "identity_exact": exact,
                                    "duration_difference": difference, "alignment": "unchecked" if difference is None else "compatible_duration" if aligned else "duration_mismatch",
                                    "eligible_for_selection": exact and aligned}})
    return found


def write_sidecar(track_path, video_path, *, out_dir=None, replace=False):
    raw = json.loads(Path(track_path).read_text(encoding="utf-8"))
    if raw.get("format") != TRACK_FORMAT:
        raise ValueError("sidecar expects a published danmaku-track/v1 JSON")
    meta = raw.get("metadata", {})
    track = make_track(raw, track_id=meta.get("track_id", "default"), label=meta.get("label"),
                       language=meta.get("language", "zh-CN"), timeline=raw.get("timeline"), bindings=raw.get("bindings"))
    video = Path(video_path).expanduser()
    if not video.name or video.suffix.lower() in (".xml", ".json", ".ass") or str(video).startswith(("http:/", "https:/")):
        raise ValueError("Specify a local video path or video filename, not a URL/comment file")
    directory = Path(out_dir).expanduser() if out_dir is not None else video.parent
    directory.mkdir(parents=True, exist_ok=True)
    last_dot = video.name.rfind(".")
    sidecar_name = (video.name[:last_dot] if last_dot > 0 else video.name) + ".xml"
    destination = directory / sidecar_name
    aligned, report = aligned_comments(track)
    with tempfile.TemporaryDirectory(prefix=".sidecar-", dir=directory) as temporary:
        staging = Path(temporary) / destination.name
        write_xml(aligned, staging)
        payload = staging.read_bytes()
        if destination.is_symlink():
            raise ValueError("Sidecar destination cannot be a symlink")
        if destination.exists() and destination.read_bytes() != payload and not replace:
            raise ValueError("A different XML already exists; choose --out-dir or explicitly use --replace")
        staging.replace(destination)
    return {"path": str(destination.resolve()), "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), **report}
