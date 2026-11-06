"""Small, auditable model adapters and an append-only usage ledger.

Only explicitly configured endpoints/keys are used. Request identity includes
all prompt content and generation settings; result reuse is not prompt caching.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


class ProviderError(RuntimeError):
    pass


_MAX_RESPONSE_BYTES = 24 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def count(value):
    return value if type(value) is int and value >= 0 else 0


def normalize_usage(raw, protocol):
    """Anthropic input excludes cache; OpenAI prompt_tokens includes cache."""
    raw = raw if isinstance(raw, dict) else {}
    nonnegative = lambda value: type(value) is int and value >= 0
    if protocol == "anthropic":
        fresh = count(raw.get("input_tokens"))
        read = count(raw.get("cache_read_input_tokens"))
        write = count(raw.get("cache_creation_input_tokens"))
        total = fresh + read + write
        output = count(raw.get("output_tokens"))
        known = all(nonnegative(raw.get(key, 0)) for key in ("cache_read_input_tokens", "cache_creation_input_tokens")) and all(nonnegative(raw.get(key)) for key in ("input_tokens", "output_tokens"))
        reasoning = count(raw.get("reasoning_tokens"))
    else:
        total = count(raw.get("prompt_tokens", raw.get("input_tokens")))
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        valid_details = isinstance(details, dict)
        details = details if isinstance(details, dict) else {}
        cached_value = details.get("cached_tokens", raw.get("prompt_cache_hit_tokens", 0))
        read = count(cached_value)
        valid_cache = nonnegative(cached_value) and read <= total
        read = min(read, total)
        write = 0
        fresh = total - read
        output = count(raw.get("completion_tokens", raw.get("output_tokens")))
        known = valid_details and valid_cache and nonnegative(raw.get("prompt_tokens", raw.get("input_tokens"))) and nonnegative(raw.get("completion_tokens", raw.get("output_tokens")))
        output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
        reasoning = count(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else 0
    return {"input_tokens": total, "fresh_input_tokens": fresh, "cached_input_tokens": read,
            "cache_write_tokens": write, "output_tokens": output, "reasoning_tokens": reasoning,
            "usage_known": known}


def price_usage(usage, pricing):
    if not usage.get("usage_known"):
        return None
    terms = [("fresh_input_tokens", "input_per_million"), ("cached_input_tokens", "cached_input_per_million"),
             ("cache_write_tokens", "cache_write_per_million"), ("output_tokens", "output_per_million")]
    result = 0.0
    for field, rate in terms:
        amount = usage.get(field, 0)
        if amount:
            unit_price = pricing.get(rate)
            if type(unit_price) not in (int, float) or not math.isfinite(unit_price) or unit_price < 0:
                return None
            result += amount * unit_price / 1_000_000
    return round(result, 10)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Provider redirected the request; check base_url. Authentication was not forwarded.")


class ModelClient:
    def __init__(self, config, run_dir, cache_dir=None):
        self.config = config
        self.run_dir = Path(run_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else self.run_dir.parent / ".cache" / "responses"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def record(self, row):
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        with self.lock:
            with (self.run_dir / "usage.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(canonical(row) + "\n")

    def _archive_response(self, payload, *, truncated=False):
        """Create one private, non-overwriting raw-body file per actual reply.

        The name is independent of both the cache key and provider request ID.
        Truncation describes a bounded/partial body read, not model stop_reason.
        """
        directory = self.run_dir / "responses" / "attempts"
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="attempt-", suffix=".json", dir=directory)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        return {"raw_response_path": (Path("responses") / "attempts" / Path(name).name).as_posix(),
                "raw_response_sha256": hashlib.sha256(payload).hexdigest(),
                "raw_response_bytes": len(payload), "raw_response_truncated": truncated}

    def invalidate(self, cache_key):
        """Discard one unusable result, retaining raw response and billing evidence.

        This does not issue a retry. An explicit rerun may make a new request.
        """
        if not isinstance(cache_key, str) or not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            return False
        with self.lock:
            path = self.cache_dir / (cache_key + ".json")
            if path.is_file():
                path.unlink()
                return True
        return False

    def complete(self, provider_alias, system, common, variant, *, stage="generation", images=None, cache_key=None):
        provider = self.config["providers"][provider_alias]
        protocol = provider["type"]
        if protocol not in ("openai_chat", "anthropic"):
            raise ProviderError(f"Unsupported provider protocol: {protocol}")
        pricing = provider.get("pricing", {})
        for field, value in pricing.items():
            if field.endswith("_per_million") and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ProviderError(f"Invalid price: {field}")
        image_blocks = []
        image_hashes = []
        for frame in images or []:
            path = Path(frame["path"])
            payload = path.read_bytes()
            if len(payload) > 12 * 1024 * 1024:
                raise ProviderError("A frame exceeds the 12 MiB request limit; reduce frame size.")
            image_hashes.append({"sha256": hashlib.sha256(payload).hexdigest(), "time": frame.get("time")})
            media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(payload).decode("ascii")
            image_blocks.append((frame.get("time"), media_type, encoded))
        settings = {key: provider.get(key) for key in ("type", "base_url", "model", "max_tokens", "temperature", "extra")}
        identity = {"version": 1, "provider": settings, "system": system, "common": common,
                    "variant": variant, "images": image_hashes, "cache_key": cache_key}
        digest = hashlib.sha256(canonical(identity).encode()).hexdigest()
        cached_file = self.cache_dir / (digest + ".json")
        base_record = {"provider": provider_alias, "requested_model": provider["model"], "stage": stage,
                       "cache_key": digest, "pricing": pricing, "billing_mode": pricing.get("billing_mode", "unknown"),
                       "currency": pricing.get("currency", "USD")}
        if cached_file.is_file():
            cached = json.loads(cached_file.read_text(encoding="utf-8"))
            if cached.get("cache_key") == digest and isinstance(cached.get("text"), str):
                self.record({**base_record, "status": "local_cache_hit", "model": cached.get("model"),
                             "local_cache_hit": True, "request_count": 0, "estimated_cost": 0,
                             "reused_usage": cached.get("usage"), "source_request_id": cached.get("request_id")})
                return {**cached, "local_cache_hit": True}
        key_name = provider.get("api_key_env")
        key = os.environ.get(key_name or "", "")
        if not key:
            raise ProviderError(f"Missing API key environment variable: {key_name or '(not configured)'}")
        content = [{"type": "text", "text": common}]
        for frame_time, media_type, encoded in image_blocks:
            content.append({"type": "text", "text": f"Frame timestamp: {frame_time} seconds."})
            if protocol == "anthropic":
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}})
            else:
                content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}})
        content.append({"type": "text", "text": variant})
        body = {"model": provider["model"], "max_tokens": provider.get("max_tokens", 2048), "stream": False}
        if provider.get("temperature") is not None:
            body["temperature"] = provider["temperature"]
        if protocol == "anthropic":
            body.update({"system": system, "messages": [{"role": "user", "content": content}]})
            endpoint = provider["base_url"].rstrip("/") + "/v1/messages"
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            body["messages"] = [{"role": "system", "content": system}, {"role": "user", "content": content}]
            endpoint = provider["base_url"].rstrip("/") + "/chat/completions"
            headers = {"Authorization": "Bearer " + key}
        extra = provider.get("extra", {})
        if any(k in extra for k in ("model", "messages", "system", "stream")):
            raise ProviderError("Provider extra cannot replace model, messages, system or stream.")
        body.update(extra)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(endpoint, data=canonical(body).encode(), headers=headers, method="POST")
        started = time.monotonic()
        raw = None
        payload = None
        archive = {}

        def retain_response(data, *, truncated=False):
            nonlocal archive
            try:
                archive = self._archive_response(data, truncated=truncated)
            except OSError as error:
                # Still account for the received usage. A successful model
                # response must not silently become an unrecorded charge when
                # the local archive cannot be written.
                archive = {"raw_response_archive_error": type(error).__name__}

        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=provider.get("timeout_seconds", 150)) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                retain_response(payload, truncated=len(payload) > _MAX_RESPONSE_BYTES)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise ProviderError("Provider response too large")
                raw = json.loads(payload)
        except Exception as error:
            code = error.code if isinstance(error, urllib.error.HTTPError) else None
            if payload is None:
                if isinstance(error, urllib.error.HTTPError):
                    try:
                        payload = error.read(_MAX_RESPONSE_BYTES + 1)
                        retain_response(payload, truncated=len(payload) > _MAX_RESPONSE_BYTES)
                    except http.client.IncompleteRead as body_error:
                        retain_response(body_error.partial, truncated=True)
                    except Exception as body_error:
                        archive = {"raw_response_read_error": type(body_error).__name__}
                elif isinstance(error, http.client.IncompleteRead):
                    retain_response(error.partial, truncated=True)
            self.record({**base_record, **archive, "status": "error", "error_type": type(error).__name__, "http_status": code,
                         "local_cache_hit": False, "request_count": 1, "estimated_cost": None,
                         "usage_known": False, "latency_seconds": round(time.monotonic() - started, 3),
                         "billing_uncertain": code is None or code >= 500})
            if isinstance(error, ProviderError):
                raise
            raise ProviderError(f"{provider_alias}: HTTP {code}" if code else f"{provider_alias}: {type(error).__name__}; request may have executed. See usage ledger.") from None
        usage = normalize_usage(raw.get("usage") if isinstance(raw, dict) else None, protocol)
        request_id = raw.get("id") if isinstance(raw, dict) else None
        request_id = request_id or uuid.uuid4().hex
        returned_model = raw.get("model") if isinstance(raw, dict) else None
        returned_model = returned_model or provider["model"]
        try:
            if not isinstance(raw, dict):
                raise ValueError("Response is not an object")
            if protocol == "anthropic":
                blocks = raw.get("content", [])
                if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
                    raise ValueError("Malformed content blocks")
                text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
                stop_reason = raw.get("stop_reason")
            else:
                choices = raw.get("choices") or []
                if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
                    raise ValueError("Malformed choices")
                message = choices[0].get("message", {}) if choices else {}
                if not isinstance(message, dict):
                    raise ValueError("Malformed message")
                text = message.get("content") or ""
                if isinstance(text, list):
                    if any(not isinstance(block, dict) for block in text):
                        raise ValueError("Malformed text blocks")
                    text = "\n".join(block.get("text", "") for block in text if block.get("type") == "text")
                stop_reason = choices[0].get("finish_reason") if choices else None
        except (ValueError, TypeError, AttributeError) as error:
            self.record({**base_record, **archive, **usage, "status": "malformed_response", "model": returned_model,
                         "request_id": request_id, "local_cache_hit": False, "request_count": 1,
                         "estimated_cost": price_usage(usage, pricing), "error_type": type(error).__name__,
                         "latency_seconds": round(time.monotonic() - started, 3)})
            write_json(self.run_dir / "responses" / (digest + ".json"), raw)
            raise ProviderError(f"{provider_alias}: malformed response; usage was retained") from None
        valid = bool(isinstance(text, str) and text.strip())
        cost = price_usage(usage, pricing)
        self.record({**base_record, **archive, **usage, "status": "completed" if valid else "empty_response", "model": returned_model,
                     "request_id": request_id, "local_cache_hit": False, "request_count": 1,
                     "estimated_cost": cost, "latency_seconds": round(time.monotonic() - started, 3), "stop_reason": stop_reason})
        # This is an ignored private run directory, never a tracked response corpus.
        write_json(self.run_dir / "responses" / (digest + ".json"), raw)
        if not valid:
            raise ProviderError(f"{provider_alias}: response contained no text (stop_reason={stop_reason})")
        if archive.get("raw_response_archive_error"):
            raise ProviderError(f"{provider_alias}: raw response archive failed; usage was retained")
        result = {"text": text, "usage": usage, "request_id": request_id, "model": returned_model,
                  "cache_key": digest, "local_cache_hit": False, "stop_reason": stop_reason}
        write_json(cached_file, result)
        return result


def parse_json_text(text):
    """Accept a JSON body or a fenced body; do not eval model output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(stripped)
