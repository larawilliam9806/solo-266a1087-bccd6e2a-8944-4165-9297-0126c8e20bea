"""Small parsing / formatting helpers shared by the web and service layers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

MONTH_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})月?$")
COMPACT_MONTH_RE = re.compile(r"^(\d{4})(\d{2})$")


class InputError(ValueError):
    """A user supplied value could not be parsed."""


def parse_month(value: object) -> str:
    """Normalise a month to ``YYYY-MM``.

    Accepts ``2026-10``, ``2026/10``, ``2026.10``, ``2026年10月`` and
    ``202610``.  Months are calendar months, so the range check matters.
    """

    raw = str(value or "").strip()
    match = MONTH_RE.match(raw) or COMPACT_MONTH_RE.match(raw)
    if not match:
        raise InputError(f"清运月格式无效：{raw or '（空）'}，应为 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise InputError(f"清运月无效：{raw}")
    if not 1900 <= year <= 2999:
        raise InputError(f"清运年份超出范围：{raw}")
    return f"{year:04d}-{month:02d}"


def next_month(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    if mon == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{mon + 1:02d}"


def parse_grams(value: object) -> int:
    """Parse a kilogram value into integer grams.

    We keep the ledger in grams so that sums are exact.  At most three
    decimal places (one gram) are accepted; silently rounding a weighbridge
    ticket would be wrong, so excess precision is rejected.
    """

    raw = str(value or "").strip()
    if not raw:
        raise InputError("净重不能为空")
    text = raw.replace("，", ",").replace(" ", "")
    if text.lower().endswith("kg"):
        text = text[:-2]
    elif text.endswith("千克"):
        text = text[: -len("千克")]
    text = text.replace(",", "")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise InputError(f"净重不是有效数字：{raw}") from None
    if not amount.is_finite():
        raise InputError(f"净重不是有效数字：{raw}")
    if amount < 0:
        raise InputError(f"净重不能为负数：{raw}")
    if amount > Decimal("1000000000000"):
        raise InputError(f"净重超出允许范围：{raw}")
    grams = amount * 1000
    if grams != grams.to_integral_value():
        raise InputError(f"净重最多支持三位小数（千克）：{raw}")
    return int(grams)


def format_kg(grams: int) -> str:
    """Format integer grams as a compact kilogram string (exact)."""

    value = Decimal(grams) / Decimal(1000)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def parse_revision(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise InputError("修订号不能为空")
    if not re.fullmatch(r"[0-9]+", raw):
        raise InputError(f"修订号必须是非负整数：{raw}")
    revision = int(raw)
    if revision < 1:
        raise InputError("修订号必须大于等于 1")
    if revision > 2_000_000_000:
        raise InputError(f"修订号过大：{raw}")
    return revision


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def display_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M UTC")
