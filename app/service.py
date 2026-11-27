"""展厅换牌业务逻辑：创建方案、发出指令、核对上报、游客查号。

状态模型
--------
* 一个「方案」把 n 件展品从现号换到目标号，另留一个空闲的临时号。
* 方案按预先算好的最少步数逐条发出「指令」；同一时刻最多一条 pending，
  核对完才能发下一条。
* 牌号事实只由「展品初始现号 + 已确认指令」推导，不另存冗余状态：
  每条确认指令把某件展品从 from_number 移到 to_number。
* 撤回不改变牌号事实（现场仍是旧号），只把该步作废并允许重发同一步。
* pending 期间，新旧两号都算「换牌中」；游客按号查讲解时，这两个号
  不给任何展品的讲解，只提示换牌中。其余号码按现牌查。
* 已确认 / 已撤回的指令再次上报，一律不改变任何业务状态。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import write_tx
from .planner import PlanError, plan_swaps

MAX_EXHIBITS = 12

_PLAIN_INT = re.compile(r"^\s*(\d{1,9})\s*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_number(text: str) -> int | None:
    """把一次现场上报解析成整数号码；无法解析则返回 None。"""
    match = _PLAIN_INT.match(text or "")
    return int(match.group(1)) if match else None


class BusinessError(Exception):
    """可展示给馆员的业务错误。"""


@dataclass(frozen=True)
class PlanSummary:
    id: int
    status: str
    temp_number: int
    total_steps: int
    next_step_index: int
    created_at: str
    completed_at: str | None


def _row_to_plan(row: sqlite3.Row) -> PlanSummary:
    return PlanSummary(
        id=row["id"],
        status=row["status"],
        temp_number=row["temp_number"],
        total_steps=len(json.loads(row["steps_json"])),
        next_step_index=row["next_step_index"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


# --------------------------------------------------------------------------
# 读取与状态推导
# --------------------------------------------------------------------------


def get_active_plan(conn: sqlite3.Connection) -> PlanSummary | None:
    row = conn.execute(
        "SELECT * FROM plans WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _row_to_plan(row) if row else None


def get_plan(conn: sqlite3.Connection, plan_id: int) -> PlanSummary | None:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return _row_to_plan(row) if row else None


def get_latest_plan(conn: sqlite3.Connection) -> PlanSummary | None:
    row = conn.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_plan(row) if row else None


def list_plans(conn: sqlite3.Connection, limit: int = 30) -> list[PlanSummary]:
    rows = conn.execute(
        "SELECT * FROM plans ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_plan(r) for r in rows]


def get_exhibits(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM exhibits WHERE plan_id = ? ORDER BY position", (plan_id,)
    ).fetchall()


def get_pending(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT i.*, e.explanation, e.position
             FROM instructions i JOIN exhibits e ON e.id = i.exhibit_id
            WHERE i.plan_id = ? AND i.status = 'pending'""",
        (plan_id,),
    ).fetchone()


def get_instructions(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.*, e.explanation, e.position
             FROM instructions i JOIN exhibits e ON e.id = i.exhibit_id
            WHERE i.plan_id = ?
            ORDER BY i.step_index, i.id""",
        (plan_id,),
    ).fetchall()


def get_reports(conn: sqlite3.Connection, instruction_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reports WHERE instruction_id = ? ORDER BY id",
        (instruction_id,),
    ).fetchall()


@dataclass(frozen=True)
class PlanState:
    locations: dict[int, sqlite3.Row]  # 号码 -> 当前摆放的展品
    exhibit_numbers: dict[int, int]  # 展品 id -> 当前所在号码
    empty_number: int  # 当前空号
    pending: sqlite3.Row | None


def compute_state(conn: sqlite3.Connection, plan: PlanSummary) -> PlanState:
    """由初始现号与已确认指令推导当前牌号状态。"""
    exhibits = get_exhibits(conn, plan.id)
    by_id = {int(e["id"]): e for e in exhibits}
    locations: dict[int, sqlite3.Row] = {
        int(e["initial_number"]): e for e in exhibits
    }
    empty = plan.temp_number
    confirmed = conn.execute(
        """SELECT * FROM instructions
            WHERE plan_id = ? AND status = 'confirmed' ORDER BY id""",
        (plan.id,),
    ).fetchall()
    for row in confirmed:
        src, dst = int(row["from_number"]), int(row["to_number"])
        locations.pop(src, None)
        locations[dst] = by_id[int(row["exhibit_id"])]
        empty = src
    pending = get_pending(conn, plan.id)
    return PlanState(
        locations=locations,
        exhibit_numbers={
            int(e["id"]): next(
                num for num, held in locations.items() if int(held["id"]) == int(e["id"])
            )
            for e in exhibits
        },
        empty_number=empty,
        pending=pending,
    )


# --------------------------------------------------------------------------
# 创建方案
# --------------------------------------------------------------------------


def create_plan(
    conn: sqlite3.Connection,
    *,
    explanations: list[str],
    current_numbers: list[int],
    target_numbers: list[int],
    temp_number: int,
) -> PlanSummary:
    if not (len(explanations) == len(current_numbers) == len(target_numbers)):
        raise BusinessError("讲解词、现号、目标号的数量必须一致。")
    cleaned = [e.strip() for e in explanations]
    if any(not e for e in cleaned):
        raise BusinessError("每件展品都必须填写讲解词。")
    try:
        steps = plan_swaps(current_numbers, target_numbers, temp_number)
    except PlanError as exc:
        raise BusinessError(str(exc)) from exc

    with write_tx(conn):
        active = conn.execute(
            "SELECT id FROM plans WHERE status = 'active'"
        ).fetchone()
        if active:
            raise BusinessError(
                f"已有进行中的方案（#{active['id']}），请先完成它再录入新方案。"
            )
        created = _now()
        steps_payload = [
            {"exhibit_index": s.exhibit_index, "from": s.from_number, "to": s.to_number}
            for s in steps
        ]
        cur = conn.execute(
            """INSERT INTO plans (status, temp_number, steps_json,
                                  next_step_index, created_at, completed_at)
               VALUES ('active', ?, ?, 0, ?, NULL)""",
            (temp_number, json.dumps(steps_payload), created),
        )
        plan_id = int(cur.lastrowid)
        for position, (explanation, current, target) in enumerate(
            zip(cleaned, current_numbers, target_numbers)
        ):
            conn.execute(
                """INSERT INTO exhibits
                     (plan_id, position, explanation, initial_number, target_number)
                   VALUES (?, ?, ?, ?, ?)""",
                (plan_id, position, explanation, current, target),
            )
        if not steps:
            # 所有展品都已在目标号，无需换牌，直接视为完成。
            conn.execute(
                "UPDATE plans SET status = 'completed', completed_at = ? WHERE id = ?",
                (created, plan_id),
            )

    plan = get_plan(conn, plan_id)
    assert plan is not None
    return plan


# --------------------------------------------------------------------------
# 发出下一条指令
# --------------------------------------------------------------------------


def issue_next_instruction(
    conn: sqlite3.Connection, plan_id: int
) -> tuple[sqlite3.Row | None, bool]:
    """若有待核对指令则原样返回（created=False）；否则发出下一步。

    返回 (指令行, 是否新建)。方案已完成或没有下一步时返回 (None, False)。
    """
    with write_tx(conn):
        plan = conn.execute(
            "SELECT * FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise BusinessError("方案不存在。")
        summary = _row_to_plan(plan)

        pending = conn.execute(
            "SELECT * FROM instructions WHERE plan_id = ? AND status = 'pending'",
            (plan_id,),
        ).fetchone()
        if pending:
            return pending, False
        if plan["status"] == "completed":
            return None, False

        steps = json.loads(plan["steps_json"])
        next_index = int(plan["next_step_index"])
        if next_index >= len(steps):
            return None, False

        state = compute_state(conn, summary)
        step = steps[next_index]
        exhibits = get_exhibits(conn, plan_id)
        exhibit = exhibits[int(step["exhibit_index"])]
        exhibit_id = int(exhibit["id"])

        from_number = state.exhibit_numbers[exhibit_id]
        to_number = int(step["to"])

        # 规划假设逐步确认；若事实与规划不符，拒绝发出并停止。
        if from_number not in state.locations or int(
            state.locations[from_number]["id"]
        ) != exhibit_id:
            raise BusinessError(
                f"第 {next_index + 1} 步出发号 {from_number} 上不是待换展品，方案状态不一致，已停止。"
            )
        if to_number in state.locations:
            raise BusinessError(
                f"第 {next_index + 1} 步目标号 {to_number} 并非空号，方案状态不一致，已停止。"
            )

        cur = conn.execute(
            """INSERT INTO instructions
                 (plan_id, step_index, exhibit_id, from_number, to_number,
                  status, observed_number, created_at, finalized_at)
               VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)""",
            (plan_id, next_index, exhibit_id, from_number, to_number, _now()),
        )
        conn.execute(
            "UPDATE plans SET next_step_index = ? WHERE id = ?",
            (next_index + 1, plan_id),
        )
        instruction = conn.execute(
            "SELECT * FROM instructions WHERE id = ?", (int(cur.lastrowid),)
        ).fetchone()
    return instruction, True


# --------------------------------------------------------------------------
# 核对上报
# --------------------------------------------------------------------------


def report_instruction(
    conn: sqlite3.Connection, instruction_id: int, reported_text: str
) -> dict:
    """处理一次现场上报。

    新号 -> 确认；旧号 -> 撤回；其他号码 -> 报错且保留待核对；
    非数字输入 -> 报错且保留待核对；
    已确认 / 已撤回的指令再次上报 -> 不做任何业务改动。
    """
    text = (reported_text or "")[:200]
    with write_tx(conn):
        instruction = conn.execute(
            "SELECT * FROM instructions WHERE id = ?", (instruction_id,)
        ).fetchone()
        if instruction is None:
            raise BusinessError("指令不存在。")
        plan_id = int(instruction["plan_id"])
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()

        if instruction["status"] != "pending":
            _record_report(
                conn, instruction_id, text, parse_number(text), "already_finalized"
            )
            return {
                "instruction_id": instruction_id,
                "result": "already_finalized",
                "instruction_status": instruction["status"],
                "changed": False,
                "message": (
                    f"该指令已处于「{instruction['status']}」，"
                    "重复上报未作任何改动，后续指令与查号结果保持原样。"
                ),
            }

        number = parse_number(text)
        if number is None:
            _record_report(conn, instruction_id, text, None, "invalid")
            return {
                "instruction_id": instruction_id,
                "result": "invalid",
                "instruction_status": "pending",
                "changed": False,
                "message": "无法识别所填内容，请填写现场看到的牌号（纯数字）。本次指令仍待核对。",
            }

        to_number = int(instruction["to_number"])
        from_number = int(instruction["from_number"])

        if number == to_number:
            conn.execute(
                """UPDATE instructions
                      SET status = 'confirmed', observed_number = ?, finalized_at = ?
                    WHERE id = ? AND status = 'pending'""",
                (number, _now(), instruction_id),
            )
            _record_report(conn, instruction_id, text, number, "confirmed")
            message = "现场已是新号，换牌已确认。"
            if _maybe_complete_plan(conn, plan):
                message += "全部展品已到位，临时号已空出，方案完成。"
            else:
                message += "可以发出下一条指令。"
            return {
                "instruction_id": instruction_id,
                "result": "confirmed",
                "instruction_status": "confirmed",
                "changed": True,
                "expected_from": from_number,
                "expected_to": to_number,
                "message": message,
            }

        if number == from_number:
            conn.execute(
                """UPDATE instructions
                      SET status = 'withdrawn', observed_number = ?, finalized_at = ?
                    WHERE id = ? AND status = 'pending'""",
                (number, _now(), instruction_id),
            )
            _record_report(conn, instruction_id, text, number, "withdrawn")
            # 该步作废，允许重发同一步：仅当它仍是最后发出的那步。
            if int(plan["next_step_index"]) == int(instruction["step_index"]) + 1:
                conn.execute(
                    "UPDATE plans SET next_step_index = ? WHERE id = ?",
                    (int(instruction["step_index"]), plan_id),
                )
            return {
                "instruction_id": instruction_id,
                "result": "withdrawn",
                "instruction_status": "withdrawn",
                "changed": True,
                "expected_from": from_number,
                "expected_to": to_number,
                "message": "现场仍是旧号，已撤回本次指令；可重新发出该步指令。",
            }

        _record_report(conn, instruction_id, text, number, "mismatch")
        return {
            "instruction_id": instruction_id,
            "result": "mismatch",
            "instruction_status": "pending",
            "changed": False,
            "expected_from": from_number,
            "expected_to": to_number,
            "message": (
                f"现场牌号 {number} 既不是新号 {to_number}，也不是旧号 {from_number}。"
                "已拒绝本次上报，指令仍待核对，请核对后重新填写。"
            ),
        }


def _record_report(
    conn: sqlite3.Connection,
    instruction_id: int,
    text: str,
    number: int | None,
    result: str,
) -> None:
    conn.execute(
        """INSERT INTO reports
             (instruction_id, reported_text, reported_number, result, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (instruction_id, text, number, result, _now()),
    )


def _maybe_complete_plan(conn: sqlite3.Connection, plan_row: sqlite3.Row) -> bool:
    steps = json.loads(plan_row["steps_json"])
    if int(plan_row["next_step_index"]) < len(steps):
        return False
    pending = conn.execute(
        "SELECT 1 FROM instructions WHERE plan_id = ? AND status = 'pending'",
        (int(plan_row["id"]),),
    ).fetchone()
    if pending:
        return False
    summary = _row_to_plan(plan_row)
    state = compute_state(conn, summary)
    exhibits = get_exhibits(conn, int(plan_row["id"]))
    if not all(
        state.exhibit_numbers[int(e["id"])] == int(e["target_number"])
        for e in exhibits
    ):
        return False
    if state.empty_number != summary.temp_number:
        return False
    conn.execute(
        "UPDATE plans SET status = 'completed', completed_at = ? WHERE id = ?",
        (_now(), summary.id),
    )
    return True


# --------------------------------------------------------------------------
# 游客查号
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LookupResult:
    query: int
    kind: str  # "found" | "swapping" | "empty" | "unknown"
    exhibit: sqlite3.Row | None = None


def resolve_lookup_plan(
    conn: sqlite3.Connection, plan_id: int | None
) -> PlanSummary | None:
    """游客页使用的方案：指定 > 进行中 > 最近一个。"""
    if plan_id is not None:
        return get_plan(conn, plan_id)
    return get_active_plan(conn) or get_latest_plan(conn)


def lookup(conn: sqlite3.Connection, plan: PlanSummary, number: int) -> LookupResult:
    """按号码查讲解；待核对指令的新旧两号一律提示换牌中。"""
    state = compute_state(conn, plan)
    if state.pending is not None:
        swapping = {
            int(state.pending["from_number"]),
            int(state.pending["to_number"]),
        }
        if number in swapping:
            return LookupResult(query=number, kind="swapping")

    exhibit = state.locations.get(number)
    if exhibit is not None:
        return LookupResult(query=number, kind="found", exhibit=exhibit)
    if number == plan.temp_number:
        return LookupResult(query=number, kind="empty")
    return LookupResult(query=number, kind="unknown")


def gallery_numbers(conn: sqlite3.Connection, plan: PlanSummary) -> list[int]:
    """游客页可查询的号码（展品号码 + 临时号），升序。"""
    numbers = {int(e["initial_number"]) for e in get_exhibits(conn, plan.id)}
    numbers.add(plan.temp_number)
    return sorted(numbers)
