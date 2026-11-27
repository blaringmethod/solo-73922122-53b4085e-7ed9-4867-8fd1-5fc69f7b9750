"""展厅换牌 Flask 应用工厂。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask

from .db import close_db, init_db

PLAN_STATUS_LABELS = {"active": "进行中", "completed": "已完成"}
INSTRUCTION_STATUS_LABELS = {"pending": "待核对", "confirmed": "已确认", "withdrawn": "已撤回"}


def _friendly_time(value: str | None) -> str:
    if not value:
        return "—"
    return value.replace("T", " ").replace("+00:00", " UTC")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.json.ensure_ascii = False

    default_db = os.environ.get("DATABASE_PATH") or str(
        Path(app.instance_path) / "gallery.db"
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=default_db,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)

    from .routes import bp

    app.register_blueprint(bp)

    app.jinja_env.filters["friendly_time"] = _friendly_time
    app.jinja_env.filters["plan_label"] = lambda value: PLAN_STATUS_LABELS.get(
        value, value
    )
    app.jinja_env.filters["ins_label"] = lambda value: INSTRUCTION_STATUS_LABELS.get(
        value, value
    )

    # 进程标识：每次进程启动随机生成，用于验收「确实换了进程」。
    app.config["BOOT_ID"] = os.environ.get("BOOT_ID") or uuid.uuid4().hex[:12]

    with app.app_context():
        init_db()

    return app
