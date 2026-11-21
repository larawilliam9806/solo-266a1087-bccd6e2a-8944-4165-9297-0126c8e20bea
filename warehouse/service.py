"""The ledger: initialization, batch upload, sealing and snapshot reads.

Rules implemented here
----------------------
* The current pending month is the only month that can be sealed.  All
  bookings land in it: rows whose collection month equals it count as
  current-month weight, older collection months count as old-month
  adjustment.  That is how late tickets and corrections to sealed months
  roll into the following report.
* A ticket number has one immutable collection month.  Its revision only
  moves forward.
* Re-sending an older revision, or the current revision with identical
  content, is a no-op.  Re-sending the current revision with changed
  content rejects the whole batch.  A higher revision is a correction:
  the previously booked version is reversed on its old street and the new
  version is booked on its new street, atomically, into the current
  pending month.
* Sealing freezes a snapshot.  Downloads and repeated seals always return
  that stored snapshot; post-seal corrections never touch it.
* Every mutation is one ``BEGIN IMMEDIATE`` transaction, so uploads and
  seals are serialised by commit order and a failed batch leaves both the
  tickets and the bottom tables exactly as they were.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

from .csvio import parse_upload
from .errors import (
    AlreadyInitializedError,
    BatchRejected,
    ConflictError,
    NotInitializedError,
    NotFoundError,
    ValidationError,
)
from .storage import read_tx, write_tx
from .utils import InputError, next_month, now_iso, parse_month

# ---------------------------------------------------------------- helpers


def _state_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM state WHERE id = 1").fetchone()


def _require_state(conn: sqlite3.Connection) -> sqlite3.Row:
    row = _state_row(conn)
    if row is None:
        raise NotInitializedError("尚未选定首个待封月，请先完成初始化")
    return row


def _sealed_months(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT month, seq, sealed_at, total_grams FROM snapshots ORDER BY seq"
    ).fetchall()


def _table_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT street,
               COALESCE(SUM(CASE WHEN collection_month = :m THEN grams END), 0)
                   AS current_grams,
               COALESCE(SUM(CASE WHEN collection_month <> :m THEN grams END), 0)
                   AS adjustment_grams
          FROM bookings
         WHERE target_month = :m
         GROUP BY street
         ORDER BY street
        """,
        {"m": month},
    ).fetchall()
    # Every street with any booking in the target month is listed, even if
    # its net is zero: a row silently disappearing would look like a lost
    # ticket to the clerk.  Zero-net streets therefore stay visible.
    return [
        {
            "street": row["street"],
            "current_grams": int(row["current_grams"] or 0),
            "adjustment_grams": int(row["adjustment_grams"] or 0),
            "total_grams": int(row["current_grams"] or 0)
            + int(row["adjustment_grams"] or 0),
        }
        for row in rows
    ]


def _payload(month: str, sealed_at: str | None, rows: list[dict]) -> dict:
    totals = {
        "current_grams": sum(row["current_grams"] for row in rows),
        "adjustment_grams": sum(row["adjustment_grams"] for row in rows),
        "total_grams": sum(row["total_grams"] for row in rows),
    }
    return {
        "schema_version": 1,
        "month": month,
        "sealed_at": sealed_at,
        "rows": rows,
        "street_count": len(rows),
        "totals": totals,
    }


def _load_json(text: str) -> dict:
    return json.loads(text)


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "").strip() or "upload.csv"
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    return base[:200]


def _batch_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "target_month": row["pending_month"],
        "uploaded_at": row["uploaded_at"],
        "row_count": row["row_count"],
        "new_count": row["new_count"],
        "corrected_count": row["corrected_count"],
        "noop_count": row["noop_count"],
        "digest": row["digest"],
    }


# ------------------------------------------------------------ state / init


def state_view(db_path: str) -> dict:
    with read_tx(db_path) as conn:
        state = _state_row(conn)
        if state is None:
            return {
                "initialized": False,
                "first_pending_month": None,
                "current_pending_month": None,
                "sealed_months": [],
                "batches": [],
            }
        sealed = [
            {
                "month": row["month"],
                "seq": row["seq"],
                "sealed_at": row["sealed_at"],
                "total_grams": row["total_grams"],
            }
            for row in _sealed_months(conn)
        ]
        batches = [
            _batch_dict(row)
            for row in conn.execute(
                "SELECT * FROM batches ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        return {
            "initialized": True,
            "first_pending_month": state["first_pending_month"],
            "current_pending_month": state["current_pending_month"],
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
            "sealed_months": sealed,
            "batches": batches,
        }


def initialize(db_path: str, month: object) -> dict:
    try:
        first = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    stamp = now_iso()
    with write_tx(db_path) as conn:
        if _state_row(conn) is not None:
            raise AlreadyInitializedError("已经选定过首个待封月，不能重复初始化")
        conn.execute(
            """
            INSERT INTO state (id, first_pending_month, current_pending_month,
                               created_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            """,
            (first, first, stamp, stamp),
        )
    return state_view(db_path)


# ------------------------------------------------------------------ upload


def upload_batch(
    db_path: str,
    filename: str,
    data: bytes,
    target_month: object | None = None,
) -> dict:
    # Cheap precondition check so "not initialised" wins over CSV errors.
    with read_tx(db_path) as conn:
        _require_state(conn)

    try:
        rows = parse_upload(data, filename)
        requested_target = parse_month(target_month) if target_month else None
    except InputError as exc:
        raise ValidationError(str(exc)) from None

    digest = hashlib.sha256(data).hexdigest()
    stamp = now_iso()

    with write_tx(db_path) as conn:
        state = _require_state(conn)
        pending = state["current_pending_month"]

        if requested_target is not None:
            if requested_target > pending:
                raise ConflictError(
                    f"批次月份 {requested_target} 尚未开启；当前待封月为 {pending}"
                )
            sealed = {
                row["month"] for row in _sealed_months(conn)
            }
            if requested_target != pending and requested_target not in sealed:
                raise ConflictError(
                    f"批次月份 {requested_target} 既不是当前待封月，也不是已封月"
                )

        seen: dict[str, int] = {}
        duplicates: list[dict] = []
        for row in rows:
            if row["ticket_no"] in seen:
                duplicates.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 在同一批次中重复"
                            f"（首次出现在第 {seen[row['ticket_no']]} 行）"
                        ),
                    }
                )
            else:
                seen[row["ticket_no"]] = row["line"]
        if duplicates:
            raise BatchRejected("批次内单号重复，整批退回", duplicates)

        existing = {}
        ticket_numbers = list(seen)
        # SQLite's default variable limit is high enough for 50k rows on
        # modern builds, but chunk to stay safely under older limits.
        for start in range(0, len(ticket_numbers), 500):
            chunk = ticket_numbers[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for record in conn.execute(
                f"SELECT * FROM tickets WHERE ticket_no IN ({placeholders})",
                chunk,
            ):
                existing[record["ticket_no"]] = record

        errors: list[dict] = []
        new_count = 0
        corrected_count = 0
        noop_count = 0

        # The batch row exists from the start so every booking can point at
        # it directly; counts are filled in before commit.
        batch_cursor = conn.execute(
            """
            INSERT INTO batches (filename, pending_month, uploaded_at,
                                 row_count, new_count, corrected_count,
                                 noop_count, digest)
            VALUES (?, ?, ?, ?, 0, 0, 0, ?)
            """,
            (_safe_filename(filename), pending, stamp, len(rows), digest),
        )
        batch_id = int(batch_cursor.lastrowid)

        for row in rows:
            collection_month = row["collection_month"]
            if collection_month > pending:
                errors.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"清运月 {collection_month} 晚于当前待封月 {pending}，"
                            "不能提前入账"
                        ),
                    }
                )
                continue

            record = existing.get(row["ticket_no"])
            if record is None:
                conn.execute(
                    """
                    INSERT INTO tickets (ticket_no, collection_month,
                                         current_revision, street,
                                         weight_grams, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["ticket_no"],
                        collection_month,
                        row["revision"],
                        row["street"],
                        row["grams"],
                        stamp,
                        stamp,
                    ),
                )
                _insert_booking(
                    conn,
                    batch_id=batch_id,
                    row=row,
                    target_month=pending,
                    street=row["street"],
                    grams=row["grams"],
                    kind="book",
                    stamp=stamp,
                )
                new_count += 1
                continue

            if record["collection_month"] != collection_month:
                errors.append(
                    {
                        "line": row["line"],
                        "ticket_no": row["ticket_no"],
                        "message": (
                            f"单号 {row['ticket_no']} 的清运月固定为 "
                            f"{record['collection_month']}，不能改为 {collection_month}"
                        ),
                    }
                )
                continue

            current_revision = int(record["current_revision"])
            if row["revision"] < current_revision:
                # An older version is superseded; keep the current values.
                noop_count += 1
                continue
            if row["revision"] == current_revision:
                if (
                    row["street"] == record["street"]
                    and row["grams"] == int(record["weight_grams"])
                ):
                    noop_count += 1
                else:
                    errors.append(
                        {
                            "line": row["line"],
                            "ticket_no": row["ticket_no"],
                            "message": (
                                f"单号 {row['ticket_no']} 提交的修订号 "
                                f"{current_revision} 已是当前版本，但街道或净重与已入账"
                                "内容不一致，整批退回"
                            ),
                        }
                    )
                continue

            # A genuinely newer revision.
            if (
                row["street"] == record["street"]
                and row["grams"] == int(record["weight_grams"])
            ):
                conn.execute(
                    "UPDATE tickets SET current_revision = ?, updated_at = ? "
                    "WHERE ticket_no = ?",
                    (row["revision"], stamp, row["ticket_no"]),
                )
                noop_count += 1
                continue

            _insert_booking(
                conn,
                batch_id=batch_id,
                row=row,
                target_month=pending,
                street=record["street"],
                grams=-int(record["weight_grams"]),
                kind="reverse",
                stamp=stamp,
                revision=current_revision,
            )
            _insert_booking(
                conn,
                batch_id=batch_id,
                row=row,
                target_month=pending,
                street=row["street"],
                grams=row["grams"],
                kind="book",
                stamp=stamp,
            )
            conn.execute(
                """
                UPDATE tickets
                   SET current_revision = ?, street = ?, weight_grams = ?,
                       updated_at = ?
                 WHERE ticket_no = ?
                """,
                (
                    row["revision"],
                    row["street"],
                    row["grams"],
                    stamp,
                    row["ticket_no"],
                ),
            )
            corrected_count += 1

        if errors:
            # Nothing has been committed: the transaction is rolled back by
            # write_tx, preserving every ticket and every bottom table.
            raise BatchRejected("批次校验未通过，整批退回，未写入任何数据", errors)

        conn.execute(
            """
            UPDATE batches
               SET new_count = ?, corrected_count = ?, noop_count = ?
             WHERE id = ?
            """,
            (new_count, corrected_count, noop_count, batch_id),
        )

        batch = _batch_dict(
            conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        )
        payload = _payload(pending, None, _table_rows(conn, pending))

    return {
        "batch": batch,
        "pending_month": pending,
        "target_month": batch["target_month"],
        "summary": {
            "rows": len(rows),
            "new": new_count,
            "corrected": corrected_count,
            "noop": noop_count,
        },
        "table": payload,
    }


def _insert_booking(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None,
    row: dict,
    target_month: str,
    street: str,
    grams: int,
    kind: str,
    stamp: str,
    revision: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO bookings (batch_id, ticket_no, revision, target_month,
                              collection_month, street, grams, kind, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            row["ticket_no"],
            row["revision"] if revision is None else revision,
            target_month,
            row["collection_month"],
            street,
            grams,
            kind,
            stamp,
        ),
    )


# -------------------------------------------------------------------- seal


def seal_month(db_path: str, month: object) -> tuple[dict, bool]:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None

    stamp = now_iso()
    with write_tx(db_path) as conn:
        state = _require_state(conn)
        pending = state["current_pending_month"]

        stored = conn.execute(
            "SELECT seq, sealed_at, payload_json FROM snapshots WHERE month = ?",
            (target,),
        ).fetchone()
        if stored is not None:
            # Re-sealing a sealed month returns the original table and does
            # not move the calendar again.
            return _load_json(stored["payload_json"]), False

        if target > pending:
            raise ConflictError(
                f"不能封存 {target}：当前待封月为 {pending}，只能封存当前待封月"
            )
        if target != pending:
            raise ConflictError(f"{target} 未处于待封状态，无法封存")

        payload = _payload(target, stamp, _table_rows(conn, pending))
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM snapshots"
        ).fetchone()["next"]
        conn.execute(
            """
            INSERT INTO snapshots (month, seq, sealed_at, total_current_grams,
                                   total_adjustment_grams, total_grams,
                                   payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target,
                seq,
                stamp,
                payload["totals"]["current_grams"],
                payload["totals"]["adjustment_grams"],
                payload["totals"]["total_grams"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            "UPDATE state SET current_pending_month = ?, updated_at = ? WHERE id = 1",
            (next_month(target), stamp),
        )
        return payload, True


# ------------------------------------------------------------------- reads


def get_snapshot(db_path: str, month: object) -> dict | None:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    with read_tx(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE month = ?", (target,)
        ).fetchone()
        return _load_json(row["payload_json"]) if row else None


def month_view(db_path: str, month: object) -> dict:
    try:
        target = parse_month(month)
    except InputError as exc:
        raise ValidationError(str(exc)) from None
    with read_tx(db_path) as conn:
        state = _require_state(conn)
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE month = ?", (target,)
        ).fetchone()
        if row is not None:
            payload = _load_json(row["payload_json"])
            return {"status": "sealed", "month": target, "table": payload}
        if target == state["current_pending_month"]:
            payload = _payload(target, None, _table_rows(conn, target))
            return {"status": "pending", "month": target, "table": payload}
        if target > state["current_pending_month"]:
            raise NotFoundError(f"{target} 尚未开启")
        raise NotFoundError(f"{target} 没有封存记录")


def snapshot_csv(db_path: str, month: object):
    from .csvio import snapshot_csv as render

    payload = get_snapshot(db_path, month)
    if payload is None:
        try:
            target = parse_month(month)
        except InputError as exc:
            raise ValidationError(str(exc)) from None
        raise ConflictError(f"{target} 尚未封存，暂无原表可下载")
    return render(payload)


def recent_batches(db_path: str, limit: int = 20) -> list[dict]:
    with read_tx(db_path) as conn:
        return [
            _batch_dict(row)
            for row in conn.execute(
                "SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]
