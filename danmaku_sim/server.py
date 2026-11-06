"""Read-only preview server: fixed web assets, selected runs and range video."""
from __future__ import annotations

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from . import __version__
from .costs import load_usage


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def public_run(directory):
    result = read_json(directory / "comments.json")
    if not isinstance(result, dict) or not isinstance(result.get("video"), dict) or not isinstance(result.get("comments"), list):
        raise ValueError("Invalid run structure")
    video = {key: result["video"][key] for key in ("id", "title", "duration", "width", "height") if key in result["video"]}
    if (directory / "media.json").exists():
        video["url"] = f"/api/runs/{directory.name}/video"
    return {"id": directory.name, "video": video, "comments": result.get("comments", []),
            "usage": load_usage(directory), "diagnostics": result.get("diagnostics", {}),
            "synthetic": True}


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = f"DanmakuPreview/{__version__}"

    def __init__(self, *args, runs_dir, web_dir, library_dir=None, catalog_only=False, **kwargs):
        self.runs_dir, self.web_dir = Path(runs_dir).resolve(), Path(web_dir).resolve()
        self.library_dir = Path(library_dir).resolve() if library_dir is not None else None
        self.catalog_only = catalog_only
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # No query strings or filesystem paths in routine request logs.
        pass

    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, path, *, download=None, published=False):
        path = Path(path)
        if not path.is_file():
            return self.json({"error": "File unavailable"}, 404)
        size = path.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not any(match.groups()):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(size - 1, int(last)) if last else size - 1
            else:
                start = max(0, size - int(last))
                end = size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".html", ".js", ".css", ".json", ".xml"):
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        length = end - start + 1 if size else 0
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if published and path.name != "catalog.json" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if published:
            self.send_header("Access-Control-Allow-Origin", "*")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/catalog/"):
                if self.library_dir is None:
                    return self.json({"error": "No published library configured"}, 404)
                relative = path.removeprefix("/catalog/")
                valid = relative == "catalog.json" or re.fullmatch(r"tracks/[0-9a-f]{64}/[A-Za-z0-9][A-Za-z0-9_-]{0,79}/[0-9a-f]{64}/comments\.(?:json|xml|ass)", relative)
                if not valid:
                    return self.json({"error": "Not found"}, 404)
                target = self.library_dir / relative
                if any(parent.is_symlink() for parent in [target, *target.parents] if parent != self.library_dir and self.library_dir in parent.parents):
                    return self.json({"error": "Not found"}, 404)
                return self.file(target, published=True)
            if self.catalog_only:
                return self.json({"error": "This service only exposes /catalog/"}, 404)
            if path in ("/", "/index.html", "/app.js", "/style.css"):
                return self.file(self.web_dir / ("index.html" if path == "/" else path.lstrip("/")))
            if path == "/api/runs":
                runs = []
                if self.runs_dir.is_dir():
                    for directory in sorted(self.runs_dir.iterdir()):
                        if not re.fullmatch(r"[A-Za-z0-9_-]+", directory.name) or directory.is_symlink() or not (directory / "comments.json").is_file():
                            continue
                        try:
                            result = read_json(directory / "comments.json")
                            if not isinstance(result, dict) or not isinstance(result.get("video"), dict) or not isinstance(result.get("comments"), list):
                                raise ValueError("Invalid run structure")
                            runs.append({"id": directory.name, "title": result.get("video", {}).get("title", directory.name),
                                "duration": result.get("video", {}).get("duration", 0), "comment_count": len(result.get("comments", [])),
                                "status": result.get("diagnostics", {}).get("status", "completed")})
                        except (ValueError, OSError, TypeError, AttributeError):
                            continue
                return self.json({"runs": runs})
            match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]+)(?:/(video|export\.(?:json|xml|ass)))?", path)
            if not match:
                return self.json({"error": "Not found"}, 404)
            name, resource = match.groups()
            directory = self.runs_dir / name
            if directory.is_symlink() or not (directory / "comments.json").is_file():
                return self.json({"error": "Run not found"}, 404)
            if not resource:
                return self.json(public_run(directory))
            if resource == "video":
                manifest = read_json(directory / "media.json")
                return self.file(manifest["path"])
            if resource == "export.json":
                exported = public_run(directory)
                exported["video"].pop("url", None)
                exported.pop("usage", None)
                exported["schema_version"] = 1
                return self.json(exported)
            return self.file(directory / resource, download=resource)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, OSError, TypeError, AttributeError):
            self.json({"error": "Run data unavailable or invalid"}, 422)


def serve(runs_dir="runs", host="127.0.0.1", port=8792, *, library_dir=None, catalog_only=False):
    if catalog_only and library_dir is None:
        raise ValueError("--catalog-only requires --library")
    web_dir = Path(__file__).resolve().parents[1] / "web"
    if not (web_dir / "index.html").exists():
        web_dir = Path(__file__).resolve().parent / "web"
    handler = partial(PreviewHandler, runs_dir=runs_dir, web_dir=web_dir, library_dir=library_dir, catalog_only=catalog_only)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Danmaku Simulator {'catalog' if catalog_only else 'preview'}: http://{host}:{port}{'/catalog/catalog.json' if catalog_only else ''} (read-only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
