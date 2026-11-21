"""HTTP-level tests: initialization, upload, seal, download, atomicity."""

import io

from warehouse import service


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


def post_upload(client, rows, target=None):
    body = {
        "file": (io.BytesIO(rows if isinstance(rows, bytes) else csv(rows)), "batch.csv")
    }
    if target:
        body["target_month"] = target
    return client.post(
        "/api/upload",
        data=body,
        headers={"X-CSRF-Token": csrf(client)},
        content_type="multipart/form-data",
    )


def csv(rows):
    text = "单号,清运月,街道,净重（千克）,修订号\n"
    text += "".join(",".join(str(part) for part in row) + "\n" for row in rows)
    return text.encode("utf-8")


def test_setup_page_and_initialize(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "选定首个待封月" in page.get_data(as_text=True)

    response = client.post(
        "/initialize",
        data={"csrf": csrf(client), "month": "2026-10"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "2026-10" in client.get("/").get_data(as_text=True)


def test_upload_requires_csrf(client, initialized):
    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(csv([("A1", "2026-10", "城东", 100, 1)])), "a.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_upload_ok_and_dashboard(client, initialized):
    response = post_upload(client, [("A1", "2026-10", "城东", "100.5", "1")])
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["pending_month"] == "2026-10"
    assert payload["summary"]["new"] == 1
    assert payload["table"]["rows"][0]["current_grams"] == 100_500

    page = client.get("/").get_data(as_text=True)
    assert "城东" in page
    assert "100.5" in page


def test_rejected_batch_returns_row_details(client, initialized):
    post_upload(client, [("A1", "2026-10", "城东", "100", "1")])
    response = post_upload(
        client,
        [
            ("B1", "2026-10", "城南", "50", "1"),
            ("A1", "2026-10", "城东", "120", "1"),
        ],
    )
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["code"] == "batch_rejected"
    assert any("A1" in item["message"] for item in payload["details"])

    # B1 and the A1 change were not written.
    state = service.month_view(initialized, "2026-10")
    streets = {row["street"] for row in state["table"]["rows"]}
    assert streets == {"城东"}
    assert state["table"]["rows"][0]["current_grams"] == 100_000


def test_seal_then_download_original(client, initialized):
    post_upload(client, [("A1", "2026-10", "城东", "100", "1")])
    response = client.post(
        "/seal",
        data={"csrf": csrf(client), "month": "2026-10"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    # Late correction into November must not touch the sealed file.
    post_upload(client, [("A1", "2026-10", "城东", "140", "2")])
    download = client.get("/month/2026-10/download")
    assert download.status_code == 200
    text = download.get_data(as_text=True)
    assert "城东" in text
    assert "100" in text
    assert "140" not in text

    page = client.get("/month/2026-10").get_data(as_text=True)
    assert "已封存" in page
    assert "100" in page


def test_download_pending_month_is_refused(client, initialized):
    post_upload(client, [("A1", "2026-10", "城东", "100", "1")])
    response = client.get("/month/2026-10/download")
    assert response.status_code == 409


def test_reseal_returns_same_table(client, initialized):
    post_upload(client, [("A1", "2026-10", "城东", "100", "1")])
    client.post("/seal", data={"csrf": csrf(client), "month": "2026-10"})
    first = client.get("/month/2026-10/download").get_data()
    post_upload(client, [("A2", "2026-09", "城东", "30", "1")])
    client.post("/seal", data={"csrf": csrf(client), "month": "2026-10"})
    second = client.get("/month/2026-10/download").get_data()
    assert first == second
    assert service.state_view(initialized)["current_pending_month"] == "2026-11"


def test_csv_with_gbk_and_bom(client, initialized):
    text = "单号,清运月,街道,净重(kg),修订号\nG1,2026-10,城北,88.25,1\n"
    response = post_upload(client, text.encode("gb18030"))
    assert response.status_code == 200, response.get_data(as_text=True)

    bom = "﻿单号,清运月,街道,净重（千克）,修订号\nG2,2026-10,城北,1,1\n"
    response = post_upload(client, bom.encode("utf-8"))
    assert response.status_code == 200, response.get_data(as_text=True)


def test_missing_columns_is_rejected(client, initialized):
    data = "单号,街道,净重\nX,城东,1\n".encode("utf-8")
    response = post_upload(client, data)
    assert response.status_code == 422
    assert "清运月" in response.get_json()["error"]


def test_bad_weight_rejects_batch(client, initialized):
    response = post_upload(client, [("A1", "2026-10", "城东", "not-a-number", 1)])
    assert response.status_code == 422
    assert response.get_json()["details"]


def test_future_collection_month_rejected_via_http(client, initialized):
    response = post_upload(client, [("A1", "2026-11", "城东", "1", 1)])
    assert response.status_code == 409
    assert response.get_json()["details"]


def test_status_api(client, initialized):
    post_upload(client, [("A1", "2026-10", "城东", "100", "1")])
    client.post("/seal", data={"csrf": csrf(client), "month": "2026-10"})
    data = client.get("/api/state").get_json()
    assert data["current_pending_month"] == "2026-11"
    assert data["sealed_months"][0]["month"] == "2026-10"
    assert data["batches"][0]["target_month"] == "2026-10"
