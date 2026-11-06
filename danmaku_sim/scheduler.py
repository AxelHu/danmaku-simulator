"""Deterministic, causal danmaku timing and explicitly synthetic repeats."""
from __future__ import annotations

from bisect import bisect_right, insort
from collections import Counter
import hashlib
import math
import random
import re
import unicodedata


def stable_seed(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("\0".join(map(str, parts)).encode()).digest()[:8], "big")


def text_key(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def _number(value: object, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return value


def _ceil_ms(value: float) -> float:
    return math.ceil((value - 1e-10) * 1000) / 1000


def _next_slot(times: list[float], desired: float, latest: float, limit: int) -> float | None:
    """Find a slot without exceeding the limit in *any* half-open one-second window.

    Checking integer seconds alone permits a burst on either side of a second
    boundary. Here every consecutive group containing the new timestamp is
    checked; jumps are to the first point that can resolve the violation.
    """
    t = _ceil_ms(desired)
    while t <= latest + 1e-9:
        index = bisect_right(times, t)
        left = max(0, index - limit)
        right = min(len(times), index + limit)
        neighbors = [(v, False) for v in times[left:index]] + [(t, True)]
        neighbors += [(v, False) for v in times[index:right]]
        next_t = t
        for start in range(max(0, len(neighbors) - limit)):
            block = neighbors[start:start + limit + 1]
            if len(block) != limit + 1 or not any(is_new for _, is_new in block):
                continue
            if block[-1][0] - block[0][0] < 1 - 1e-9:
                first_existing = min(v for v, is_new in block if not is_new)
                next_t = max(next_t, _ceil_ms(first_existing + 1))
        if next_t == t:
            return t
        t = max(next_t, _ceil_ms(t + .001))
    return None


def schedule_comments(candidates: list[dict], duration: float, config: dict | None = None,
                      *, seed: int = 42) -> tuple[list[dict], dict]:
    """Schedule candidates; configuration is the ``scheduler`` section.

    ``max_repeat_copies`` is the number of additional occurrences of an exact
    normalized text per source event, counting independent model agreement too.
    ``echo_max_chars`` and ``echo_max_parent_delay`` only constrain artificial
    repeats; independent viewers can still coincidentally use the same text.
    A target rate is a whole-video ceiling; it never creates filler to meet it.
    The returned diagnostics count every dropped input and added repeat.
    """
    config = config or {}
    duration = _number(duration, "duration", 0.001)
    reaction_min = _number(config.get("reaction_min", .35), "reaction_min")
    reaction_max = _number(config.get("reaction_max", 3.8), "reaction_max", reaction_min)
    max_delay = _number(config.get("max_delay", 8), "max_delay", reaction_min)
    limit_raw = _number(config.get("max_per_second", 3), "max_per_second", 1)
    copies_raw = _number(config.get("max_repeat_copies", 2), "max_repeat_copies")
    echo_chars_raw = _number(config.get("echo_max_chars", 8), "echo_max_chars", 1)
    echo_max_parent_delay = _number(config.get("echo_max_parent_delay", 2), "echo_max_parent_delay")
    if not limit_raw.is_integer() or not copies_raw.is_integer():
        raise ValueError("max_per_second and max_repeat_copies must be integers")
    if not echo_chars_raw.is_integer():
        raise ValueError("echo_max_chars must be an integer")
    limit, max_copies = int(limit_raw), int(copies_raw)
    echo_max_chars = int(echo_chars_raw)
    repeat_ratio = _number(config.get("repeat_ratio", .12), "repeat_ratio")
    if repeat_ratio > 1:
        raise ValueError("repeat_ratio must be between 0 and 1")
    target = config.get("target_per_minute")
    max_total = math.inf if target is None else math.ceil(duration * _number(target, "target_per_minute") / 60)
    dropped: Counter = Counter()
    planned: list[tuple[float, float, dict]] = []
    seen_ids: set[str] = set()
    for index, original in enumerate(candidates):
        candidate = dict(original)
        try:
            cutoff = _number(candidate["knowledge_cutoff"], "knowledge_cutoff")
            event_at = _number(candidate.get("event_available_at", cutoff), "event_available_at")
            text = candidate["text"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty text")
            if cutoff < event_at - 1e-9:
                dropped["future_event"] += 1
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            dropped["invalid_candidate"] += 1
            continue
        candidate_id = str(candidate.get("id") or f"candidate_{index}")
        if candidate_id in seen_ids:
            dropped["duplicate_id"] += 1
            continue
        seen_ids.add(candidate_id)
        candidate["id"] = candidate_id
        earliest = cutoff + reaction_min
        latest = min(cutoff + max_delay, duration - .001)
        if _ceil_ms(earliest) > latest + 1e-9:
            dropped["no_time_remaining"] += 1
            continue
        rng = random.Random(stable_seed(seed, candidate_id, "reaction"))
        desired = cutoff + rng.uniform(reaction_min, min(reaction_max, max_delay))
        planned.append((min(desired, latest), latest, candidate))
    planned.sort(key=lambda row: (row[0], row[2]["id"]))
    if len(planned) > max_total:
        count = int(max_total)
        # Spread a small viewing budget across the entire video, not just its opening.
        selected = {int((i + .5) * len(planned) / count) for i in range(count)} if count else set()
        dropped["target_rate"] += len(planned) - len(selected)
        planned = [row for index, row in enumerate(planned) if index in selected]

    comments: list[dict] = []
    occupied: list[float] = []
    occurrences: Counter = Counter()
    delayed = 0
    for desired, latest, candidate in planned:
        key = (str(candidate.get("event_id", "")), text_key(candidate["text"]))
        if occurrences[key] >= max_copies + 1:
            dropped["repetition_cap"] += 1
            continue
        actual = _next_slot(occupied, desired, latest, limit)
        if actual is None:
            dropped["density_overflow"] += 1
            continue
        delayed += actual > _ceil_ms(desired) + 1e-9
        comment = {
            "id": candidate["id"], "time": actual, "text": candidate["text"],
            "mode": 1, "color": 16777215, "size": 25,
            "persona": str(candidate.get("persona", "viewer")),
            "virtual_viewer_id": str(candidate.get("virtual_viewer_id", candidate.get("persona", "viewer"))),
            "provider": str(candidate.get("provider", "")), "model": str(candidate.get("model", "")),
            "event_id": str(candidate.get("event_id", "")), "kind": str(candidate.get("kind", "reaction")),
            "knowledge_cutoff": float(candidate["knowledge_cutoff"]),
            "event_available_at": float(candidate.get("event_available_at", candidate["knowledge_cutoff"])),
            "round": candidate.get("round", 1), "synthetic": True,
            "generation_method": "independent", "end_time": duration,
        }
        insort(occupied, actual)
        occurrences[key] += 1
        comments.append(comment)

    originals = list(comments)
    echo_budget = min(math.floor(len(originals) * repeat_ratio), max(0, max_total - len(originals)))
    echoes = 0
    echo_kind_pool = [c for c in originals if c["kind"] in {"reaction", "emotion", "joke"}]
    echo_pool = [c for c in echo_kind_pool if len(c["text"]) <= echo_max_chars]
    echo_parents_too_long = len(echo_kind_pool) - len(echo_pool)
    echo_pool.sort(key=lambda c: stable_seed(seed, c["id"], "echo_priority"))
    for parent in echo_pool:
        if echoes >= echo_budget:
            break
        key = (parent["event_id"], text_key(parent["text"]))
        if occurrences[key] >= max_copies + 1:
            continue
        rng = random.Random(stable_seed(seed, parent["id"], "echo"))
        latest = min(duration - .001, parent["knowledge_cutoff"] + max_delay,
                     parent["time"] + echo_max_parent_delay)
        desired = parent["time"] + rng.uniform(.25, 1.4)
        actual = _next_slot(occupied, desired, latest, limit)
        if actual is None:
            dropped["echo_no_room"] += 1
            continue
        echo = dict(parent)
        suffix = hashlib.sha256(f"{seed}:{parent['id']}:echo".encode()).hexdigest()[:12]
        echo.update(id=f"echo_{suffix}", time=actual, parent_id=parent["id"], kind="echo",
                    persona=f"{parent['persona']}:echo:{suffix}",
                    virtual_viewer_id=f"echo_viewer_{suffix}", generation_method="controlled_repeat")
        comments.append(echo)
        insort(occupied, actual)
        occurrences[key] += 1
        echoes += 1
    comments.sort(key=lambda item: (item["time"], item["id"]))
    return comments, {
        "input_candidates": len(candidates), "independent_comments": len(originals),
        "echo_budget": int(echo_budget), "echoes_added": echoes, "output_comments": len(comments),
        "echo_max_chars": echo_max_chars, "echo_max_parent_delay": echo_max_parent_delay,
        "echo_eligible_parents": len(echo_pool), "echo_parents_too_long": echo_parents_too_long,
        "delayed_for_density": delayed, "dropped": dict(sorted(dropped.items())),
        "density_rule": "any half-open 1-second interval", "max_per_second": limit,
        "target_per_minute": target, "max_repeat_copies": max_copies,
    }


schedule = schedule_comments
