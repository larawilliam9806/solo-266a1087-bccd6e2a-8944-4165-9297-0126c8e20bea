"""Rule-by-rule tests for the ledger service."""

import pytest

from warehouse import service
from warehouse.errors import BatchRejected, ConflictError, NotFoundError


def rows_for(service_mod, db_path, month):
    view = service_mod.month_view(db_path, month)
    return {row["street"]: row for row in view["table"]["rows"]}


def upload(db_path, rows, target=None):
    from tests.conftest import csv_bytes

    header = "单号,清运月,街道,净重（千克）,修订号"
    return service.upload_batch(
        db_path, "batch.csv", csv_bytes(*rows, header=header), target_month=target
    )


def test_initialize_and_month_rollover(db_path):
    service.initialize(db_path, "2026年10月")
    state = service.state_view(db_path)
    assert state["current_pending_month"] == "2026-10"

    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    payload, advanced = service.seal_month(db_path, "2026-10")
    assert advanced is True
    assert service.state_view(db_path)["current_pending_month"] == "2026-11"
    assert payload["totals"]["current_grams"] == 100_000


def test_duplicate_initialize_rejected(db_path):
    service.initialize(db_path, "2026-10")
    with pytest.raises(Exception):
        service.initialize(db_path, "2026-11")


def test_late_ticket_splits_current_and_adjustment(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    service.seal_month(db_path, "2026-10")
    # Late ticket for the sealed month, uploaded into November.
    upload(db_path, [("A2", "2026-09", "城东", "30", "1")])

    view = service.month_view(db_path, "2026-11")
    row = view["table"]["rows"][0]
    assert row["current_grams"] == 0
    assert row["adjustment_grams"] == 30_000
    assert row["total_grams"] == 30_000

    # The sealed October snapshot is untouched.
    sealed = service.get_snapshot(db_path, "2026-10")
    assert sealed["totals"]["total_grams"] == 100_000


def test_correction_reverses_old_street_and_books_new(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    service.seal_month(db_path, "2026-10")

    # November correction: street change and weight change, revision 2.
    upload(db_path, [("A1", "2026-10", "城西", "120", "2")])
    view = service.month_view(db_path, "2026-11")
    table = {row["street"]: row for row in view["table"]["rows"]}
    # The old version is reversed on its original street, then the corrected
    # version is booked on the corrected street.
    assert table["城东"]["adjustment_grams"] == -100_000
    assert table["城东"]["total_grams"] == -100_000
    assert table["城西"]["current_grams"] == 0
    assert table["城西"]["adjustment_grams"] == 120_000
    assert view["table"]["totals"]["total_grams"] == 20_000


def test_correction_same_street_nets_delta(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    service.seal_month(db_path, "2026-10")
    upload(db_path, [("A1", "2026-10", "城东", "80", "2")])
    row = service.month_view(db_path, "2026-11")["table"]["rows"][0]
    assert row["street"] == "城东"
    assert row["adjustment_grams"] == -20_000


def test_lower_revision_is_noop(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "3")])
    result = upload(db_path, [("A1", "2026-10", "城东", "999", "2")])
    assert result["summary"]["noop"] == 1
    row = service.month_view(db_path, "2026-10")["table"]["rows"][0]
    assert row["current_grams"] == 100_000


def test_current_revision_same_content_is_noop(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    result = upload(db_path, [("A1", "2026-10", "城东", "100.000", "1")])
    assert result["summary"]["noop"] == 1


def test_current_revision_changed_content_rejects_whole_batch(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    with pytest.raises(BatchRejected) as excinfo:
        upload(
            db_path,
            [
                ("B1", "2026-10", "城南", "50", "1"),
                ("A1", "2026-10", "城东", "999", "1"),
            ],
        )
    assert excinfo.value.details
    table = {row["street"]: row for row in service.month_view(db_path, "2026-10")["table"]["rows"]}
    assert "城南" not in table
    assert table["城东"]["current_grams"] == 100_000


def test_collection_month_is_immutable(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    with pytest.raises(BatchRejected):
        upload(db_path, [("A1", "2026-10", "城东", "100", "2")])


def test_future_collection_month_rejected(initialized):
    db_path = initialized
    with pytest.raises(BatchRejected):
        upload(db_path, [("A1", "2026-12", "城东", "100", "1")])


def test_reseal_returns_original_snapshot(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    first, advanced = service.seal_month(db_path, "2026-10")
    assert advanced
    upload(db_path, [("A2", "2026-09", "城东", "30", "1")])
    again, advanced_again = service.seal_month(db_path, "2026-10")
    assert advanced_again is False
    assert again == first
    assert service.state_view(db_path)["current_pending_month"] == "2026-11"


def test_cannot_seal_future_month(initialized):
    with pytest.raises(ConflictError):
        service.seal_month(initialized, "2026-12")


def test_missing_month_is_not_found(initialized):
    upload(initialized, [("A1", "2026-10", "城东", "100", "1")])
    service.seal_month(initialized, "2026-10")
    with pytest.raises(NotFoundError):
        service.month_view(initialized, "2020-01")


def test_duplicate_ticket_in_one_batch_rejected(initialized):
    db_path = initialized
    with pytest.raises(BatchRejected) as excinfo:
        upload(
            db_path,
            [
                ("A1", "2026-10", "城东", "10", "1"),
                ("A1", "2026-10", "城东", "11", "2"),
            ],
        )
    assert "重复" in excinfo.value.details[0]["message"]


def test_revision_advances_without_value_change(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    result = upload(db_path, [("A1", "2026-10", "城东", "100", "2")])
    assert result["summary"]["noop"] == 1
    # Revision 2 is now current, so revision 2 with different content must fail.
    with pytest.raises(BatchRejected):
        upload(db_path, [("A1", "2026-10", "城东", "101", "2")])


def test_same_revision_as_old_but_different_from_current_is_noop(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    upload(db_path, [("A1", "2026-10", "城东", "90", "2")])
    result = upload(db_path, [("A1", "2026-10", "城东", "100", "1")])
    assert result["summary"]["noop"] == 1
    row = service.month_view(db_path, "2026-10")["table"]["rows"][0]
    assert row["current_grams"] == 90_000

    # A revision that never existed cannot be "stale": revision 5 with new
    # content is a normal forward correction even from revision 2.
    result = upload(db_path, [("A1", "2026-10", "城西", "70", "5")])
    assert result["summary"]["corrected"] == 1


def test_zero_net_weight_ticket_is_allowed(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-10", "城东", "0", "1")])
    # A zero booking is still visible on its street row.
    rows = service.month_view(db_path, "2026-10")["table"]["rows"]
    assert rows == [
        {
            "street": "城东",
            "current_grams": 0,
            "adjustment_grams": 0,
            "total_grams": 0,
        }
    ]
    conn_rows = service.state_view(db_path)["batches"]
    assert conn_rows[0]["new_count"] == 1


def test_adjustment_can_drive_street_negative(initialized):
    db_path = initialized
    upload(db_path, [("A1", "2026-09", "城东", "100", "1")])
    service.seal_month(db_path, "2026-10")
    upload(db_path, [("A1", "2026-09", "城东", "10", "2")])
    row = service.month_view(db_path, "2026-11")["table"]["rows"][0]
    assert row["adjustment_grams"] == -90_000
    assert row["total_grams"] == -90_000
