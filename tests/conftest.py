import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warehouse import create_app  # noqa: E402
from warehouse import service  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    from warehouse.storage import ensure_schema

    path = str(tmp_path / "ledger.db")
    ensure_schema(path)
    return path


@pytest.fixture()
def app(db_path):
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": db_path,
            "SECRET_KEY": "test-secret",
        }
    )
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def csv_bytes(*rows, header="单号,清运月,街道,净重（千克）,修订号"):
    lines = [header]
    for row in rows:
        lines.append(",".join(str(part) for part in row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def upload(client, rows, target=None, **kwargs):
    data = {"file": (__import__("io").BytesIO(csv_bytes(*rows)), "batch.csv")}
    if target is not None:
        data["target_month"] = target
    return client.post(
        "/api/upload",
        data=data,
        headers={"X-CSRF-Token": _csrf(client)},
        content_type="multipart/form-data",
        **kwargs,
    )


def _csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf"]


@pytest.fixture()
def initialized(app, db_path):
    service.initialize(db_path, "2026-10")
    return db_path
