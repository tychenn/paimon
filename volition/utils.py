from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any


def now_ts() -> int:
    return int(time.time())


def stable_hash(*parts: Any) -> str:
    raw = "||".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def slugify(text: str) -> str:
    clean = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(text or "").strip().lower())
    clean = re.sub(r"-{2,}", "-", clean).strip("-")
    return clean or "unknown"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_json_loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def strip_code_fence(text: str) -> str:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    return clean.strip()


def extract_json_object(text: str) -> dict[str, Any] | None:
    clean = strip_code_fence(text)
    try:
        value = json.loads(clean)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(clean[start : end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def format_ts(ts: int | float | None) -> str:
    try:
        value = int(ts or 0)
    except Exception:
        value = 0
    if value <= 0:
        return "从未"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def truncate(text: str, limit: int = 96) -> str:
    clean = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    return clean if len(clean) <= limit else clean[: max(1, limit - 3)] + "..."
