"""HTTP 路由：馆员工作台 + 游客查号页。"""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import service
from .db import get_db
from .service import BusinessError

bp = Blueprint("main", __name__)

MAX_EXHIBITS = service.MAX_EXHIBITS


def _wants_json() -> bool:
    if request.args.get("format") == "json":
        return True
    if request.is_json:
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json"


def _plan_context(conn, plan) -> dict:
    state = service.compute_state(conn, plan)
    exhibits = service.get_exhibits(conn, plan.id)
    rows = [
        {
            "id": int(e["id"]),
            "position": int(e["position"]),
            "explanation": e["explanation"],
            "initial": int(e["initial_number"]),
            "target": int(e["target_number"]),
            "current": state.exhibit_numbers[int(e["id"])],
        }
        for e in exhibits
    ]
    return {
        "plan": plan,
        "state": state,
        "rows": rows,
        "pending": state.pending,
        "instructions": service.get_instructions(conn, plan.id),
        "can_issue": state.pending is None and plan.status == "active",
        "empty_number": state.empty_number,
    }


@bp.get("/")
def index():
    return redirect(url_for("main.clerk"))


@bp.get("/healthz")
def healthz():
    return jsonify({"ok": True, "boot_id": current_app.config["BOOT_ID"]})


@bp.get("/clerk")
def clerk():
    conn = get_db()
    active = service.get_active_plan(conn)
    plan = active or service.get_latest_plan(conn)
    context = _plan_context(conn, plan) if plan else None
    return render_template(
        "clerk.html",
        active=active,
        context=context,
        plans=service.list_plans(conn),
    )


@bp.get("/clerk/plans/new")
def new_plan():
    return render_template("new_plan.html", max_exhibits=MAX_EXHIBITS)


def _parse_exhibit_rows(form) -> tuple[list[str], list[int], list[int]]:
    explanations = form.getlist("explanation")
    currents = form.getlist("current_number")
    targets = form.getlist("target_number")
    if not (len(explanations) == len(currents) == len(targets)):
        raise BusinessError("表单内容不完整，请刷新后重试。")
    rows: list[tuple[str, str, str]] = []
    for explanation, current, target in zip(explanations, currents, targets):
        explanation = explanation.strip()
        current = current.strip()
        target = target.strip()
        if not (explanation or current or target):
            continue  # 整行空白，视为未使用
        rows.append((explanation, current, target))
    if not rows:
        raise BusinessError("请至少填写一件展品。")
    if len(rows) > MAX_EXHIBITS:
        raise BusinessError(f"最多只能录入 {MAX_EXHIBITS} 件展品。")

    clean_explanations: list[str] = []
    clean_currents: list[int] = []
    clean_targets: list[int] = []
    for index, (explanation, current, target) in enumerate(rows, start=1):
        if not explanation:
            raise BusinessError(f"第 {index} 行缺少讲解词。")
        if not current.isdigit() or not target.isdigit():
            raise BusinessError(f"第 {index} 行的现号和目标号都必须是正整数。")
        clean_explanations.append(explanation)
        clean_currents.append(int(current))
        clean_targets.append(int(target))
    return clean_explanations, clean_currents, clean_targets


@bp.post("/clerk/plans")
def create_plan():
    conn = get_db()
    try:
        explanations, currents, targets = _parse_exhibit_rows(request.form)
        temp_raw = request.form.get("temp_number", "").strip()
        if not temp_raw.isdigit():
            raise BusinessError("临时号必须是正整数。")
        plan = service.create_plan(
            conn,
            explanations=explanations,
            current_numbers=currents,
            target_numbers=targets,
            temp_number=int(temp_raw),
        )
    except BusinessError as exc:
        flash(str(exc), "error")
        return (
            render_template(
                "new_plan.html",
                max_exhibits=MAX_EXHIBITS,
                form=request.form,
            ),
            400,
        )

    flash(
        f"方案 #{plan.id} 已创建：共 {plan.total_steps} 次换牌（最少步数），临时号 {plan.temp_number}。",
        "success",
    )
    return redirect(url_for("main.plan_detail", plan_id=plan.id))


@bp.get("/clerk/plans/<int:plan_id>")
def plan_detail(plan_id: int):
    conn = get_db()
    plan = service.get_plan(conn, plan_id)
    if plan is None:
        return render_template("not_found.html", message="方案不存在。"), 404
    context = _plan_context(conn, plan)
    return render_template("plan.html", **context)


@bp.post("/clerk/plans/<int:plan_id>/instructions")
def issue_instruction(plan_id: int):
    conn = get_db()
    try:
        instruction, created = service.issue_next_instruction(conn, plan_id)
    except BusinessError as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": str(exc)}), 409
        flash(str(exc), "error")
        return redirect(url_for("main.plan_detail", plan_id=plan_id))

    if instruction is None:
        payload = {
            "ok": True,
            "created": False,
            "instruction": None,
            "message": "方案已完成，没有更多指令。",
        }
        if _wants_json():
            return jsonify(payload)
        flash(payload["message"], "info")
        return redirect(url_for("main.plan_detail", plan_id=plan_id))

    if _wants_json():
        return (
            jsonify(
                {
                    "ok": True,
                    "created": created,
                    "instruction": {
                        "id": int(instruction["id"]),
                        "step_index": int(instruction["step_index"]),
                        "from_number": int(instruction["from_number"]),
                        "to_number": int(instruction["to_number"]),
                        "status": instruction["status"],
                    },
                    "message": (
                        "已发出新指令。"
                        if created
                        else "已有待核对指令，未重复发出。"
                    ),
                }
            ),
            201 if created else 200,
        )
    flash(
        "已发出新指令，请现场换牌并核对。"
        if created
        else "已有待核对指令，请先完成核对。",
        "info",
    )
    return redirect(url_for("main.instruction_detail", instruction_id=int(instruction["id"])))


@bp.get("/clerk/instructions/<int:instruction_id>")
def instruction_detail(instruction_id: int):
    conn = get_db()
    instruction = conn.execute(
        """SELECT i.*, e.explanation, e.position, e.target_number
             FROM instructions i JOIN exhibits e ON e.id = i.exhibit_id
            WHERE i.id = ?""",
        (instruction_id,),
    ).fetchone()
    if instruction is None:
        return render_template("not_found.html", message="指令不存在。"), 404
    plan = service.get_plan(conn, int(instruction["plan_id"]))
    reports = service.get_reports(conn, instruction_id)
    return render_template(
        "instruction.html",
        instruction=instruction,
        plan=plan,
        reports=reports,
    )


@bp.post("/clerk/instructions/<int:instruction_id>/report")
def report_instruction(instruction_id: int):
    conn = get_db()
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("number", ""))
    else:
        text = request.form.get("number", "")
    try:
        result = service.report_instruction(conn, instruction_id, text)
    except BusinessError as exc:
        if _wants_json():
            return jsonify({"ok": False, "error": str(exc)}), 404
        flash(str(exc), "error")
        return redirect(url_for("main.clerk"))

    if _wants_json():
        return jsonify({"ok": True, **result})

    flash(result["message"], _flash_category(result["result"]))
    return redirect(
        url_for("main.instruction_detail", instruction_id=instruction_id)
    )


def _flash_category(result: str) -> str:
    if result in {"confirmed", "withdrawn"}:
        return "success"
    if result == "already_finalized":
        return "info"
    return "error"


@bp.get("/gallery")
def gallery():
    conn = get_db()
    try:
        plan_id = int(request.args["plan"]) if request.args.get("plan") else None
    except ValueError:
        plan_id = None
    plan = service.resolve_lookup_plan(conn, plan_id)
    if plan is None:
        return render_template("gallery.html", plan=None, numbers=[], result=None)
    numbers = service.gallery_numbers(conn, plan)
    return render_template(
        "gallery.html",
        plan=plan,
        numbers=numbers,
        result=None,
        query=None,
    )


@bp.get("/gallery/lookup")
def gallery_lookup():
    conn = get_db()
    raw = request.args.get("number", "").strip()
    plan_id_raw = request.args.get("plan", "").strip()
    plan = service.resolve_lookup_plan(
        conn, int(plan_id_raw) if plan_id_raw.isdigit() else None
    )
    if plan is None:
        return render_template(
            "gallery.html", plan=None, numbers=[], result=None, query=raw
        )
    numbers = service.gallery_numbers(conn, plan)

    if not raw.isdigit():
        return (
            render_template(
                "gallery.html",
                plan=plan,
                numbers=numbers,
                result=None,
                query=raw,
                error="请输入墙上的号码（纯数字）。",
            ),
            400,
        )

    result = service.lookup(conn, plan, int(raw))
    return render_template(
        "gallery.html",
        plan=plan,
        numbers=numbers,
        result=result,
        query=raw,
    )
