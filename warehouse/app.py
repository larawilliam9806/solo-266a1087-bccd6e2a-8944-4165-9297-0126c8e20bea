"""Flask application for the weighbridge monthly close ledger."""

from __future__ import annotations

import hmac
import io
import os
import secrets

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import service
from .errors import LedgerError, NotFoundError, ValidationError
from .storage import ensure_schema, resolve_db_path
from .utils import InputError, display_time, format_kg, parse_month


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    db_path = app.config.get("DB_PATH") or resolve_db_path()
    app.config["DB_PATH"] = db_path
    ensure_schema(db_path)

    app.jinja_env.filters["kg"] = format_kg
    app.jinja_env.filters["time"] = display_time

    # ------------------------------------------------------------ helpers

    def csrf_token() -> str:
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    def check_csrf() -> None:
        expected = session.get("csrf")
        supplied = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="页面校验令牌失效，请刷新后重试")

    app.jinja_env.globals["csrf_token"] = csrf_token

    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.is_json

    @app.errorhandler(LedgerError)
    def handle_ledger_error(exc: LedgerError):
        if wants_json():
            return jsonify(_error_payload(exc)), exc.status_code
        return _error_page(db_path, exc.message, exc.details, exc.status_code)

    @app.errorhandler(400)
    def handle_400(exc):
        message = getattr(exc, "description", None) or "请求无效"
        if wants_json():
            return jsonify({"ok": False, "error": message, "code": "bad_request"}), 400
        return _error_page(db_path, message, [], 400)

    @app.errorhandler(404)
    def handle_404(exc):
        if wants_json():
            return jsonify({"ok": False, "error": "未找到", "code": "not_found"}), 404
        return _error_page(db_path, "页面或月份不存在", [], 404)

    @app.errorhandler(413)
    def handle_413(exc):
        message = "上传文件超过 8 MB 上限，请拆分后分批上传"
        if wants_json():
            return jsonify({"ok": False, "error": message, "code": "too_large"}), 413
        return _error_page(db_path, message, [], 413)

    # ------------------------------------------------------------- routes

    @app.get("/")
    def dashboard():
        state = service.state_view(db_path)
        if not state["initialized"]:
            return render_template("setup.html", state=state)
        month = request.args.get("month")
        view = None
        if month:
            try:
                view = service.month_view(db_path, month)
            except NotFoundError:
                view = None
        if view is None:
            view = service.month_view(db_path, state["current_pending_month"])
        return render_template(
            "dashboard.html",
            state=state,
            view=view,
            current_month=state["current_pending_month"],
        )

    @app.post("/initialize")
    def initialize():
        check_csrf()
        service.initialize(db_path, request.form.get("month"))
        return redirect(url_for("dashboard"))

    @app.post("/api/upload")
    def upload():
        check_csrf()
        upload_file = request.files.get("file")
        if upload_file is None or not upload_file.filename:
            raise ValidationError("请选择要上传的 CSV 文件")
        result = service.upload_batch(
            db_path,
            upload_file.filename,
            upload_file.read(),
            target_month=request.form.get("target_month") or None,
        )
        return jsonify({"ok": True, **result})

    @app.post("/seal")
    def seal():
        check_csrf()
        payload, advanced = service.seal_month(db_path, request.form.get("month"))
        return redirect(
            url_for(
                "month_detail",
                month=payload["month"],
                result="sealed" if advanced else "unchanged",
            )
        )

    @app.get("/month/<month>")
    def month_detail(month: str):
        view = service.month_view(db_path, month)
        state = service.state_view(db_path)
        return render_template(
            "month.html",
            state=state,
            view=view,
            current_month=state["current_pending_month"],
            result=request.args.get("result"),
        )

    @app.get("/month/<month>/download")
    def download(month: str):
        try:
            target = parse_month(month)
        except InputError as exc:
            raise ValidationError(str(exc)) from None
        data = service.snapshot_csv(db_path, target)
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=f"底表-{target}.csv",
        )

    @app.get("/template.csv")
    def template():
        data = (
            "单号,清运月,街道,净重（千克）,修订号\n"
            "WB-2026-0001,2026-10,城东街道,1280.5,1\n"
            "WB-2026-0002,2026-09,城南街道,980,2\n"
        ).encode("utf-8-sig")
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="入仓模板.csv",
        )

    @app.get("/api/state")
    def api_state():
        return jsonify(service.state_view(db_path))

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


def _error_payload(exc: LedgerError) -> dict:
    return {
        "ok": False,
        "error": exc.message,
        "code": exc.code,
        "details": exc.details[:100],
        "detail_count": len(exc.details),
        "details_truncated": len(exc.details) > 100,
    }


def _error_page(db_path: str, message: str, details, status: int):
    try:
        state = service.state_view(db_path)
    except Exception:  # pragma: no cover - the error page must never fail
        state = {
            "initialized": False,
            "first_pending_month": None,
            "current_pending_month": None,
            "sealed_months": [],
            "batches": [],
        }
    return (
        render_template(
            "error.html",
            state=state,
            message=message,
            details=details[:100],
            detail_count=len(details),
            status=status,
        ),
        status,
    )
