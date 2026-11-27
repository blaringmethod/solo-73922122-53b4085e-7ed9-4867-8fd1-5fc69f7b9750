import sqlite3

import pytest

from app import create_app
from app.planner import minimal_step_count, plan_swaps


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {"DATABASE": str(tmp_path / "test.db"), "TESTING": True, "SECRET_KEY": "t"}
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    with app.app_context():
        from app.db import get_db

        yield get_db()


def create(client, rows, temp):
    data = {
        "explanation": [r[2] for r in rows],
        "current_number": [str(r[0]) for r in rows],
        "target_number": [str(r[1]) for r in rows],
        "temp_number": str(temp),
    }
    return client.post("/clerk/plans", data=data, follow_redirects=False)


def issue(client, plan_id):
    return client.post(f"/clerk/plans/{plan_id}/instructions", follow_redirects=False)


def report(client, instruction_id, value):
    return client.post(
        f"/clerk/instructions/{instruction_id}/report",
        data={"number": str(value)},
        follow_redirects=False,
    )


def pending_id(conn, plan_id):
    row = conn.execute(
        "SELECT id FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)
    ).fetchone()
    return row["id"] if row else None


def test_plan_hits_minimum_and_produces_valid_sequence():
    cur = [3, 1, 2, 5, 4]
    tgt = [1, 2, 3, 4, 5]
    steps = plan_swaps(cur, tgt, 6)
    # 循环 (0,2,1) 长 3 -> 4 步；循环 (3,4) 长 2 -> 3 步。
    assert len(steps) == minimal_step_count(cur, tgt, 6) == 7

    where = dict(enumerate(cur))
    empty = 6
    for step in steps:
        assert where[step.exhibit_index] == step.from_number
        assert step.to_number == empty
        where[step.exhibit_index] = step.to_number
        empty = step.from_number
    assert [where[i] for i in range(len(cur))] == tgt
    assert empty == 6


def test_full_swap_confirms_and_completes(client, conn):
    r = create(
        client,
        [(1, 2, "甲"), (2, 1, "乙")],
        temp=9,
    )
    assert r.status_code == 302
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])

    r = issue(client, plan_id)
    assert r.status_code == 302
    ins = pending_id(conn, plan_id)
    row = conn.execute("SELECT * FROM instructions WHERE id=?", (ins,)).fetchone()

    # 游客：两号都换牌中，临时号此时正是该步的新号（同样换牌中）。
    assert client.get("/gallery/lookup?number=1").status_code == 200
    html = client.get("/gallery/lookup?number=1").get_data(as_text=True)
    assert "换牌中" in html
    html = client.get("/gallery/lookup?number=2").get_data(as_text=True)
    assert "换牌中" in html
    # 既非展品号也非临时号的号码：查无此号
    html = client.get("/gallery/lookup?number=7").get_data(as_text=True)
    assert "没有这个号码" in html

    # 错误号码：保留待核对
    r = report(client, ins, 5)
    assert r.status_code == 302
    assert pending_id(conn, plan_id) == ins

    # 新号：确认
    r = report(client, ins, row["to_number"])
    status = conn.execute("SELECT status FROM instructions WHERE id=?", (ins,)).fetchone()[0]
    assert status == "confirmed"

    # 待核对清除后才能发下一步
    r = issue(client, plan_id)
    ins2 = pending_id(conn, plan_id)
    assert ins2 and ins2 != ins
    row2 = conn.execute("SELECT * FROM instructions WHERE id=?", (ins2,)).fetchone()

    # 重复上报已确认指令：不改变任何东西
    r = report(client, ins, row["to_number"])
    assert r.status_code == 302
    assert pending_id(conn, plan_id) == ins2

    report(client, ins2, row2["to_number"])

    # 互换共 3 步，还差最后一步
    issue(client, plan_id)
    ins3 = pending_id(conn, plan_id)
    row3 = conn.execute("SELECT * FROM instructions WHERE id=?", (ins3,)).fetchone()
    report(client, ins3, row3["to_number"])

    plan = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["status"] == "completed"


def test_withdraw_then_reissue(client, conn):
    r = create(client, [(1, 2, "甲"), (2, 1, "乙")], temp=9)
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    issue(client, plan_id)
    ins = pending_id(conn, plan_id)
    row = conn.execute("SELECT * FROM instructions WHERE id=?", (ins,)).fetchone()

    report(client, ins, row["from_number"])
    assert conn.execute("SELECT status FROM instructions WHERE id=?", (ins,)).fetchone()[0] == "withdrawn"
    assert pending_id(conn, plan_id) is None

    # 撤回后重发同一步（新指令，同一步号）
    issue(client, plan_id)
    ins2 = pending_id(conn, plan_id)
    row2 = conn.execute("SELECT * FROM instructions WHERE id=?", (ins2,)).fetchone()
    assert row2["step_index"] == row["step_index"]

    # 已撤回的再报：不改动
    report(client, ins, row["from_number"])
    assert pending_id(conn, plan_id) == ins2


def test_mismatch_and_invalid_keep_pending(client, conn):
    r = create(client, [(1, 2, "甲"), (2, 1, "乙")], temp=9)
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    issue(client, plan_id)
    ins = pending_id(conn, plan_id)
    report(client, ins, 777)
    assert pending_id(conn, plan_id) == ins
    client.post(f"/clerk/instructions/{ins}/report", data={"number": "abc"})
    assert pending_id(conn, plan_id) == ins
    statuses = [r["result"] for r in conn.execute("SELECT result FROM reports WHERE instruction_id=? ORDER BY id", (ins,))]
    assert statuses[-2:] == ["mismatch", "invalid"]


def test_validation_rejects_bad_numbers(client, conn):
    r = create(client, [(1, 1, "甲"), (1, 2, "乙")], temp=9)  # 现号重复
    assert r.status_code == 400
    r = create(client, [(1, 2, "甲"), (2, 3, "乙")], temp=9)  # 号码组不一致
    assert r.status_code == 400
    r = create(client, [(1, 2, "甲"), (2, 1, "乙")], temp=1)  # 临时号冲突
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_restart_resumes_pending(tmp_path):
    db = str(tmp_path / "restart.db")
    app1 = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "t"})
    c1 = app1.test_client()
    r = c1.post(
        "/clerk/plans",
        data={
            "explanation": ["甲", "乙"],
            "current_number": ["1", "2"],
            "target_number": ["2", "1"],
            "temp_number": "9",
        },
    )
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    c1.post(f"/clerk/plans/{plan_id}/instructions")

    # 重新创建应用（模拟重启）
    app2 = create_app({"DATABASE": db, "TESTING": True, "SECRET_KEY": "t"})
    c2 = app2.test_client()
    html = c2.get("/clerk").get_data(as_text=True)
    assert "待核对指令" in html
    with app2.app_context():
        from app.db import get_db

        conn2 = get_db()
        ins = conn2.execute("SELECT * FROM instructions WHERE status='pending'").fetchone()
        from_number, to_number = ins["from_number"], ins["to_number"]
        # 重启后按现场号码继续：看到新号 -> 确认
        c2.post(f"/clerk/instructions/{ins['id']}/report", data={"number": str(to_number)})
        assert conn2.execute("SELECT status FROM instructions WHERE id=?", (ins["id"],)).fetchone()[0] == "confirmed"
        # 互换共 3 步：继续把剩下两步走完
        for _ in range(2):
            c2.post(f"/clerk/plans/{plan_id}/instructions")
            ins2 = conn2.execute("SELECT * FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)).fetchone()
            assert ins2 is not None
            c2.post(f"/clerk/instructions/{ins2['id']}/report", data={"number": str(ins2["to_number"])})
        assert conn2.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0] == "completed"


def test_lookup_by_plate_after_confirmation(client, conn):
    r = create(client, [(1, 2, "甲讲解"), (2, 1, "乙讲解")], temp=9)
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    issue(client, plan_id)
    ins = pending_id(conn, plan_id)
    row = conn.execute("SELECT * FROM instructions WHERE id=?", (ins,)).fetchone()
    report(client, ins, row["to_number"])  # 甲 1 -> 9 已确认

    # 第 1 步确认后、下一步待核对前：9 号上已是甲，2 号仍是乙。
    html = client.get(f"/gallery/lookup?number=9&plan={plan_id}").get_data(as_text=True)
    assert "甲讲解" in html
    html = client.get(f"/gallery/lookup?number=2&plan={plan_id}").get_data(as_text=True)
    assert "乙讲解" in html

    # 发出第 2 步（乙 2 -> 1）待核对：2 与 1 都提示换牌中；9 仍按现牌查。
    issue(client, plan_id)
    html = client.get(f"/gallery/lookup?number=2&plan={plan_id}").get_data(as_text=True)
    assert "换牌中" in html
    html = client.get(f"/gallery/lookup?number=1&plan={plan_id}").get_data(as_text=True)
    assert "换牌中" in html
    html = client.get(f"/gallery/lookup?number=9&plan={plan_id}").get_data(as_text=True)
    assert "甲讲解" in html


def test_noop_plan_completes_immediately(client, conn):
    r = create(client, [(1, 1, "甲"), (2, 2, "乙")], temp=9)
    assert r.status_code == 302
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert row["status"] == "completed"
    # 游客仍可按现牌查询
    html = client.get(f"/gallery/lookup?number=1&plan={plan_id}").get_data(as_text=True)
    assert "甲" in html


def test_json_api_and_single_pending(client, conn):
    r = client.post(
        "/clerk/plans?format=json",
        json={
            "explanation": ["甲", "乙"],
            "current_number": ["1", "2"],
            "target_number": ["2", "1"],
            "temp_number": "9",
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code in (302, 308, 415, 400)  # 该表单端点仅接受表单
    r = create(client, [(1, 2, "甲"), (2, 1, "乙")], temp=9)
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    first = client.post(f"/clerk/plans/{plan_id}/instructions?format=json")
    assert first.status_code == 201
    body = first.json
    assert body["created"] is True
    second = client.post(f"/clerk/plans/{plan_id}/instructions?format=json")
    assert second.status_code == 200
    assert second.json["created"] is False
    assert second.json["instruction"]["id"] == body["instruction"]["id"]
    # 核对完才能发下一条
    ins = body["instruction"]
    rep = client.post(
        f"/clerk/instructions/{ins['id']}/report?format=json",
        json={"number": ins["to_number"]},
    )
    assert rep.status_code == 200
    assert rep.json["result"] == "confirmed"
    nxt = client.post(f"/clerk/plans/{plan_id}/instructions?format=json")
    assert nxt.json["created"] is True


def test_withdraw_middle_step_keeps_confirmed_progress(client, conn):
    # 4 件、两个 2-循环；确认第 1 步后，在第 2 步撤回，再重走。
    r = create(
        client,
        [(1, 2, "甲"), (2, 1, "乙"), (3, 4, "丙"), (4, 3, "丁")],
        temp=5,
    )
    plan_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    issue(client, plan_id)
    first = conn.execute(
        "SELECT * FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)
    ).fetchone()
    report(client, first["id"], first["to_number"])

    issue(client, plan_id)
    second = conn.execute(
        "SELECT * FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)
    ).fetchone()
    report(client, second["id"], second["from_number"])
    assert conn.execute(
        "SELECT status FROM instructions WHERE id=?", (second["id"],)
    ).fetchone()[0] == "withdrawn"

    # 撤回后同一位置重发，出发号依然是现场实际的号
    issue(client, plan_id)
    third = conn.execute(
        "SELECT * FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)
    ).fetchone()
    assert third["step_index"] == second["step_index"]
    assert third["from_number"] == second["from_number"]
    assert third["to_number"] == second["to_number"]

    # 继续走完
    report(client, third["id"], third["to_number"])
    for _ in range(10):
        issue(client, plan_id)
        ins = conn.execute(
            "SELECT * FROM instructions WHERE plan_id=? AND status='pending'", (plan_id,)
        ).fetchone()
        if ins is None:
            break
        report(client, ins["id"], ins["to_number"])
    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "completed"
    # 最终按号查讲解：每个号对应目标展品
    expected = {2: "甲", 1: "乙", 4: "丙", 3: "丁", 5: "临时空号"}
    for number, keyword in expected.items():
        html = client.get(f"/gallery/lookup?number={number}&plan={plan_id}").get_data(
            as_text=True
        )
        assert keyword in html, (number, keyword)


def test_second_active_plan_rejected(client, conn):
    create(client, [(1, 2, "甲"), (2, 1, "乙")], temp=9)
    r = create(client, [(1, 2, "丙"), (2, 1, "丁")], temp=8)
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
