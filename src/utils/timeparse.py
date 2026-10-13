"""
Normalisation of Xiaohongshu's human-readable timestamps into ISO 8601.

Xiaohongshu renders publish times as relative or year-less strings, e.g.
    "刚刚"  "12分钟前"  "3小时前"  "昨天"  "前天"  "5天前"  "08-19"  "2024-08-19"

Storing those verbatim would make the `created_at` field useless for incremental
comparison or time-series analysis, so we resolve them against the collection
moment. The resolution rules are deliberately explicit and unit-tested, because
"08-19" is genuinely ambiguous (it means the most recent August 19th that is not
in the future).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_CJK_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _to_int(token: str) -> Optional[int]:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if token in _CJK_NUM:
        return _CJK_NUM[token]
    # Handles forms like "十五" / "二十" for the small ranges XHS actually emits.
    if len(token) == 2 and token[0] == "十" and token[1] in _CJK_NUM:
        return 10 + _CJK_NUM[token[1]]
    if len(token) == 2 and token[1] == "十" and token[0] in _CJK_NUM:
        return _CJK_NUM[token[0]] * 10
    if len(token) == 3 and token[0] in _CJK_NUM and token[1] == "十" and token[2] in _CJK_NUM:
        return _CJK_NUM[token[0]] * 10 + _CJK_NUM[token[2]]
    return None


def parse_xhs_time(text: str, now: Optional[datetime] = None) -> str:
    """
    Convert a Xiaohongshu time label into an ISO 8601 UTC timestamp.

    Returns an empty string when the label cannot be interpreted, so callers can
    report the gap honestly rather than silently inventing a timestamp.
    """
    if not text:
        return ""

    label = re.sub(r"\s+", "", str(text))
    label = label.replace("编辑于", "").replace("发布于", "")
    if not label:
        return ""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    if label in ("刚刚", "刚才", "now"):
        resolved = reference

    elif label in ("昨天", "昨日"):
        resolved = reference - timedelta(days=1)

    elif label in ("前天",):
        resolved = reference - timedelta(days=2)

    else:
        match = re.fullmatch(r"(\d+|[一二两三四五六七八九十]+)(分钟|小时|天|周|个月|月)前", label)
        if match:
            amount = _to_int(match.group(1))
            if amount is None:
                return ""
            unit = match.group(2)
            deltas = {
                "分钟": timedelta(minutes=amount),
                "小时": timedelta(hours=amount),
                "天": timedelta(days=amount),
                "周": timedelta(weeks=amount),
                "个月": timedelta(days=30 * amount),
                "月": timedelta(days=30 * amount),
            }
            resolved = reference - deltas[unit]

        else:
            full = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", label)
            short = re.fullmatch(r"(\d{1,2})-(\d{1,2})", label)
            try:
                if full:
                    year, month, day = (int(g) for g in full.groups())
                elif short:
                    month, day = (int(g) for g in short.groups())
                    # Year-less dates mean the most recent occurrence that is not in
                    # the future, so "12-25" seen in October refers to last year.
                    candidate = datetime(reference.year, month, day, tzinfo=timezone.utc)
                    year = reference.year if candidate <= reference else reference.year - 1
                else:
                    return ""
                resolved = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                # e.g. "02-30" -- not a real calendar date.
                return ""

    return resolved.astimezone(timezone.utc).isoformat()
