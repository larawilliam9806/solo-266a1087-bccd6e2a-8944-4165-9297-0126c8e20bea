"""CSV decoding, header mapping, row parsing and snapshot export."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata

from .errors import ValidationError
from .utils import format_kg, parse_grams, parse_month, parse_revision, InputError

MAX_ROWS = 50_000
MAX_BYTES = 8 * 1024 * 1024

# Normalised header -> canonical field.  Normalisation strips spaces,
# underscores, dashes and brackets and applies NFKC, so "净重（千克）",
# "净重 (kg)" and "NET_WEIGHT" all collapse to a predictable key.
HEADER_ALIASES = {
    "ticketno": "ticket_no",
    "ticketnumber": "ticket_no",
    "ticket": "ticket_no",
    "单号": "ticket_no",
    "磅单号": "ticket_no",
    "票据号": "ticket_no",
    "单据号": "ticket_no",
    "流水号": "ticket_no",
    "清运月": "collection_month",
    "清运月份": "collection_month",
    "清运年月": "collection_month",
    "月份": "collection_month",
    "collectionmonth": "collection_month",
    "collection月": "collection_month",
    "month": "collection_month",
    "街道": "street",
    "所属街道": "street",
    "街道办": "street",
    "社区": "street",
    "片区": "street",
    "street": "street",
    "净重": "weight",
    "净重千克": "weight",
    "净重kg": "weight",
    "净重量": "weight",
    "重量": "weight",
    "重量千克": "weight",
    "weight": "weight",
    "netweight": "weight",
    "净重公斤": "weight",
    "修订号": "revision",
    "版本号": "revision",
    "版本": "revision",
    "修订": "revision",
    "revision": "revision",
    "rev": "revision",
    "version": "revision",
}

REQUIRED = ("ticket_no", "collection_month", "street", "weight", "revision")


def _normalise_header(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"[\s_\-()（）\[\]【】]+", "", text)


def _decode(data: bytes) -> str:
    if not data:
        raise ValidationError("CSV 文件为空")
    if len(data) > MAX_BYTES:
        raise ValidationError("CSV 文件超过 8 MB 上限，请拆分后分批上传")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ValidationError("UTF-16 CSV 无法解码") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别 CSV 编码，请使用 UTF-8 或 GB18030")


def _sniff_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return dialect.delimiter
    except csv.Error:
        return ","


def _clean(value: str) -> str:
    return (value or "").strip()


def _clean_street(value: str) -> str:
    text = re.sub(r"\s+", " ", _clean(value))
    if not text:
        raise InputError("街道不能为空")
    if len(text) > 200:
        raise InputError("街道名称过长")
    if any(ord(ch) < 32 for ch in text):
        raise InputError("街道名称含控制字符")
    return text


def _clean_ticket(value: str) -> str:
    text = _clean(value)
    if not text:
        raise InputError("单号不能为空")
    if len(text) > 128:
        raise InputError("单号过长")
    if any(ord(ch) < 32 for ch in text):
        raise InputError("单号含控制字符")
    return text


def parse_upload(data: bytes, filename: str = "upload.csv") -> list[dict]:
    """Parse and validate every row; never returns a partially valid batch."""

    text = _decode(data)
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=_sniff_delimiter(text[:8192]))
    header: list[str] | None = None
    columns: dict[str, int] = {}
    rows: list[dict] = []
    errors: list[dict] = []

    for raw in reader:
        line = reader.line_num
        if header is None:
            if not raw or all(not _clean(cell) for cell in raw):
                continue
            header = raw
            normalised = [_normalise_header(cell) for cell in raw]
            for index, key in enumerate(normalised):
                field = HEADER_ALIASES.get(key)
                if field and field not in columns:
                    columns[field] = index
            missing = [name for name in REQUIRED if name not in columns]
            if missing:
                labels = {
                    "ticket_no": "单号",
                    "collection_month": "清运月",
                    "street": "街道",
                    "weight": "净重（千克）",
                    "revision": "修订号",
                }
                raise ValidationError(
                    "CSV 表头缺少列：" + "、".join(labels[name] for name in missing)
                    + "。需要的表头为：单号、清运月、街道、净重（千克）、修订号"
                )
            continue

        if not raw or all(not _clean(cell) for cell in raw):
            continue
        if len(rows) >= MAX_ROWS:
            raise ValidationError(f"单批最多 {MAX_ROWS} 行，请拆分后上传")
        if len(raw) > len(header) and any(_clean(cell) for cell in raw[len(header):]):
            errors.append({"line": line, "message": "列数多于表头，无法确定归属列"})
            continue

        def cell(field: str) -> str:
            index = columns[field]
            return raw[index] if index < len(raw) else ""

        try:
            ticket_no = _clean_ticket(cell("ticket_no"))
            collection_month = parse_month(cell("collection_month"))
            street = _clean_street(cell("street"))
            grams = parse_grams(cell("weight"))
            revision = parse_revision(cell("revision"))
        except InputError as exc:
            errors.append({"line": line, "message": str(exc)})
            continue

        rows.append(
            {
                "line": line,
                "ticket_no": ticket_no,
                "collection_month": collection_month,
                "street": street,
                "grams": grams,
                "revision": revision,
            }
        )

    if header is None:
        raise ValidationError("CSV 没有表头行")
    if errors:
        raise ValidationError("CSV 存在无法解析的行，整批退回", errors)
    if not rows:
        raise ValidationError("CSV 没有数据行")
    return rows


def rows_digest(rows: list[dict]) -> str:
    canonical = "\n".join(
        "\x1f".join(
            [
                row["ticket_no"],
                row["collection_month"],
                row["street"],
                str(row["grams"]),
                str(row["revision"]),
            ]
        )
        for row in rows
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_csv(payload: dict) -> bytes:
    """Render the sealed bottom table exactly as it was frozen."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["街道", "当月净重（千克）", "旧月调整（千克）", "合计（千克）"])
    for row in payload["rows"]:
        writer.writerow(
            [
                row["street"],
                format_kg(row["current_grams"]),
                format_kg(row["adjustment_grams"]),
                format_kg(row["total_grams"]),
            ]
        )
    totals = payload["totals"]
    writer.writerow(
        [
            "合计",
            format_kg(totals["current_grams"]),
            format_kg(totals["adjustment_grams"]),
            format_kg(totals["total_grams"]),
        ]
    )
    return ("﻿" + buffer.getvalue()).encode("utf-8")
