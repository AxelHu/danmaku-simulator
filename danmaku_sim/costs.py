"""Usage aggregation keeps measured tokens separate from reference prices."""
from __future__ import annotations
from collections import defaultdict
import json
from pathlib import Path

FIELDS = ("input_tokens", "fresh_input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "audio_seconds")


def summarize(records):
    rows = list(records)
    currencies = {r.get("currency", "USD") for r in rows}
    if len(currencies) > 1:
        raise ValueError("Mixed currencies need explicit conversion; refusing to sum them.")
    result = {key: 0 for key in FIELDS}
    result.update({"currency": next(iter(currencies), "USD"), "request_count": 0, "local_cache_hits": 0,
                   "estimated_cost": 0.0, "payg_estimated_cost": 0.0, "subscription_reference_cost": 0.0,
                   "unpriced_requests": 0, "failed_requests": 0, "actual_cost": None})
    modes = set()
    for row in rows:
        if row.get("local_cache_hit"):
            result["local_cache_hits"] += 1
            continue
        for field in FIELDS:
            value = row.get(field, 0)
            if type(value) in (int, float):
                result[field] += value
        result["request_count"] += row.get("request_count", 1)
        if row.get("status") not in ("completed", "no_speech"):
            result["failed_requests"] += 1
        mode = row.get("billing_mode", "unknown")
        modes.add(mode)
        cost = row.get("estimated_cost")
        if type(cost) not in (int, float):
            result["unpriced_requests"] += 1
        else:
            result["estimated_cost"] += cost
            if mode == "subscription":
                result["subscription_reference_cost"] += cost
            elif mode == "payg":
                result["payg_estimated_cost"] += cost
    result["billing_mode"] = next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "none"
    for key in ("estimated_cost", "payg_estimated_cost", "subscription_reference_cost"):
        result[key] = round(result[key], 8)
    result["cache_hit_ratio"] = result["cached_input_tokens"] / result["input_tokens"] if result["input_tokens"] else None
    result["cost_complete"] = result["unpriced_requests"] == 0
    return result


def load_usage(run_dir):
    path = Path(run_dir) / "usage.jsonl"
    rows = []
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    raise ValueError(f"Usage ledger has an invalid line {number}; partial writes must be inspected.") from None
    summary = summarize(rows)
    models = defaultdict(list)
    stages = defaultdict(list)
    for row in rows:
        models[(row.get("provider", "unknown"), row.get("model") or row.get("requested_model", "unknown"))].append(row)
        stages[row.get("stage", "unknown")].append(row)
    summary["by_model"] = [{"provider": provider, "model": model, **summarize(records)} for (provider, model), records in models.items()]
    summary["by_stage"] = [{"stage": stage, **summarize(records)} for stage, records in stages.items()]
    return {"summary": summary}
