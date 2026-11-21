"""Concurrency and crash-safety tests.

The guarantee under test is: concurrent uploads and seals are serialised by
SQLite's write lock, so each batch lands in exactly one month, decided by
commit order.  A failed or interrupted batch must leave tickets, bookings and
sealed snapshots untouched.
"""

import threading

import pytest

from warehouse import service
from warehouse.errors import BatchRejected


def csv_bytes(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(p) for p in row) + "\n" for row in rows)
    return text.encode("utf-8")


def test_concurrent_uploads_and_seal_are_serialised(db_path):
    service.initialize(db_path, "2026-10")
    barrier = threading.Barrier(8)
    outcomes = []
    errors = []
    lock = threading.Lock()

    def uploader(index):
        try:
            barrier.wait(timeout=10)
            result = service.upload_batch(
                db_path,
                f"b{index}.csv",
                csv_bytes([(f"T{index}", "2026-09", "城东", 10 + index, 1)]),
            )
            with lock:
                outcomes.append(("upload", index, result["pending_month"]))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def sealer():
        try:
            barrier.wait(timeout=10)
            service.seal_month(db_path, "2026-10")
            with lock:
                outcomes.append(("seal", 0, "2026-10"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=uploader, args=(i,)) for i in range(7)]
    threads.append(threading.Thread(target=sealer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(outcomes) == 8

    total = 0
    for month in ("2026-10", "2026-11"):
        snap = service.get_snapshot(db_path, month)
        if snap:
            total += snap["totals"]["total_grams"]
    pending = service.month_view(db_path, "2026-11")["table"]["totals"]["total_grams"]
    sealed = service.get_snapshot(db_path, "2026-10")
    sealed_total = sealed["totals"]["total_grams"] if sealed else 0
    assert sealed_total + pending == sum(10 + i for i in range(7)) * 1000

    # Each upload landed in exactly one month, and none were lost.
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        for kind, index, month in outcomes:
            if kind != "upload":
                continue
            booked = conn.execute(
                "SELECT target_month, SUM(grams) FROM bookings "
                "WHERE ticket_no = ? GROUP BY target_month",
                (f"T{index}",),
            ).fetchall()
            assert len(booked) == 1, (index, booked)
            assert booked[0][0] == month, (index, month, booked)
            assert booked[0][1] == (10 + index) * 1000
    finally:
        conn.close()


def test_failed_batch_is_fully_atomic(db_path):
    service.initialize(db_path, "2026-10")
    service.upload_batch(
        db_path, "a.csv", csv_bytes([("A1", "2026-10", "城东", 100, 1)])
    )
    before = service.month_view(db_path, "2026-10")["table"]

    with pytest.raises(BatchRejected):
        service.upload_batch(
            db_path,
            "bad.csv",
            csv_bytes(
                [
                    ("NEW1", "2026-10", "城西", 50, 1),
                    ("A1", "2026-10", "城东", 999, 1),  # current rev, changed
                ]
            ),
        )

    after = service.month_view(db_path, "2026-10")["table"]
    assert before == after
    assert service.state_view(db_path)["batches"][0]["filename"] == "a.csv"


def test_correction_is_atomic_when_reverse_and_book_are_one_transaction(db_path):
    """Both halves of a correction share a transaction, so a crash cannot
    leave the old weight reversed without the new weight booked."""

    service.initialize(db_path, "2026-10")
    service.upload_batch(
        db_path, "a.csv", csv_bytes([("A1", "2026-10", "城东", 100, 1)])
    )
    service.seal_month(db_path, "2026-10")

    class Boom(Exception):
        pass

    original = service._insert_booking
    calls = {"n": 0}

    def exploding(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # after the reversal, before the re-booking
            raise Boom()
        return original(conn, **kwargs)

    service._insert_booking = exploding
    try:
        with pytest.raises(Boom):
            service.upload_batch(
                db_path, "b.csv", csv_bytes([("A1", "2026-10", "城西", 120, 2)])
            )
    finally:
        service._insert_booking = original

    nov = service.month_view(db_path, "2026-11")["table"]
    assert nov["rows"] == []

    # And a retry now applies both halves.
    service.upload_batch(
        db_path, "b.csv", csv_bytes([("A1", "2026-10", "城西", 120, 2)])
    )
    nov = service.month_view(db_path, "2026-11")["table"]
    totals = {row["street"]: row["total_grams"] for row in nov["rows"]}
    assert totals == {"城东": -100_000, "城西": 120_000}
