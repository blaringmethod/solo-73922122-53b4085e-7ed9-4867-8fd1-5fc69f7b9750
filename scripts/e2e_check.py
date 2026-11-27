#!/usr/bin/env python3
"""端到端验收脚本：对运行中的换牌网页走一遍真实 HTTP 流程。

用法：
    BASE_URL=http://127.0.0.1:8099 python3 scripts/e2e_check.py

覆盖：
  1. 录入 4 件展品（两个循环），校验最少步数。
  2. 逐步发出指令、故意填错号码、确认 / 撤回。
  3. 游客查号：待核对时新旧号「换牌中」，其余号按现牌。
  4. 中断续办：停在待核对指令，由 RESTART_CMD 启新进程（同库），
     并用 /healthz 的 boot_id 确认确实换了进程，再接着办。
  5. 重复上报已处理指令不改变状态。
退出码 0 表示全部通过。

RESTART_CMD 约定：执行后 BASE_URL 上应是一个新进程（同一数据库）。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8099").rstrip("/")
RESTART_CMD = os.environ.get("RESTART_CMD", "").strip()

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

_checks = 0
_failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}  {detail}")
        _failures.append(label)


def request(path: str, data: dict | None = None, headers: dict | None = None):
    url = BASE + path
    body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with _opener.open(req, timeout=15) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.geturl(), exc.read().decode("utf-8")


def post_json(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def extract_plan_id(location: str) -> int:
    tail = location.rstrip("/").split("/")[-1]
    return int(tail)


def health_boot_id() -> str:
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=10) as resp:
            return json.loads(resp.read().decode()).get("boot_id", "")
    except (urllib.error.URLError, ValueError):
        return ""


def main() -> int:
    print(f"BASE_URL = {BASE}")
    print("1) 录入方案：4 件展品，号码 1-4，临时号 5，目标为两个互换循环")

    rows = [
        ("青铜鼎，商代晚期，用于祭祀", "1", "2"),
        ("青瓷瓶，宋代，民窑酒器", "2", "1"),
        ("玉璧，汉代，礼器", "3", "4"),
        ("木雕佛像，明代，民间供奉", "4", "3"),
    ]
    status, location, html = request(
        "/clerk/plans",
        {
            "explanation": [r[0] for r in rows],
            "current_number": [r[1] for r in rows],
            "target_number": [r[2] for r in rows],
            "temp_number": "5",
        },
    )
    check(status == 200 and "/clerk/plans/" in location, "创建方案返回 302 到方案页", f"{status} {location}")
    plan_id = extract_plan_id(location)
    check("最少换牌次数" in html, "方案页显示最少换牌次数")
    check("临时号" in html, "方案页显示临时号")

    print("2) 第 1 步：发出指令并核对游客查号")
    status, body = post_json(f"/clerk/plans/{plan_id}/instructions", {})
    check(status == 201 and body["created"], "发出第 1 条指令", str(body))
    ins1 = body["instruction"]
    check(ins1["from_number"] == 1 and ins1["to_number"] == 5,
          "第 1 步为 1 -> 5（借临时号）", str(ins1))

    status, body2 = post_json(f"/clerk/plans/{plan_id}/instructions", {})
    check(status == 200 and body2["instruction"]["id"] == ins1["id"] and not body2["created"],
          "待核对时不会重复发出新指令", str(body2))

    html = request(f"/gallery/lookup?number=1&plan={plan_id}")[2]
    check("换牌中" in html, "游客查 1 号：换牌中")
    html = request(f"/gallery/lookup?number=5&plan={plan_id}")[2]
    check("换牌中" in html, "游客查 5 号（新号）：换牌中")
    html = request(f"/gallery/lookup?number=2&plan={plan_id}")[2]
    check("青瓷瓶" in html, "游客查 2 号：仍按现牌给出讲解")

    print("3) 上报：错号保留、旧号撤回、新号确认")
    status, result = post_json(f"/clerk/instructions/{ins1['id']}/report", {"number": 3})
    check(result["result"] == "mismatch" and result["instruction_status"] == "pending",
          "填错号 -> mismatch 且仍待核对", str(result))
    html = request(f"/gallery/lookup?number=1&plan={plan_id}")[2]
    check("换牌中" in html, "错号上报后 1 号仍显示换牌中")

    status, result = post_json(f"/clerk/instructions/{ins1['id']}/report", {"number": 1})
    check(result["result"] == "withdrawn", "看到旧号 -> 撤回本次指令", str(result))
    html = request(f"/gallery/lookup?number=1&plan={plan_id}")[2]
    check("青铜鼎" in html, "撤回后 1 号恢复正常查号")

    # 重发同一步（撤回允许重试）
    status, body = post_json(f"/clerk/plans/{plan_id}/instructions", {})
    check(body["created"], "撤回后可重新发出该步", str(body))
    ins1b = body["instruction"]
    check(ins1b["step_index"] == ins1["step_index"], "重发的仍是同一步", str(ins1b))

    print("4) 中断续办：停在第 1 步待核对，换新进程继续")
    boot_before = health_boot_id()
    check(bool(boot_before), "重启前能读到进程标识", str(boot_before))
    if RESTART_CMD:
        old_base = BASE
        subprocess.run(RESTART_CMD, shell=True, check=True)
        # RESTART_CMD 可把新地址写到 E2E_BASE_FILE；否则沿用原地址。
        new_base_file = os.environ.get("E2E_BASE_FILE", "")
        if new_base_file and os.path.exists(new_base_file):
            new_base = open(new_base_file, encoding="utf-8").read().strip()
            globals()["BASE"] = new_base.rstrip("/")
            print(f"   新进程地址：{BASE}（原 {old_base}）")
        boot_after = health_boot_id()
        check(
            bool(boot_after) and boot_after != boot_before,
            "重启后确实是另一个进程（boot_id 变化）",
            f"{boot_before} -> {boot_after}",
        )
    html = request("/clerk")[2]
    check("待核对指令" in html, "重启后工作台仍有待核对指令")
    status, result = post_json(f"/clerk/instructions/{ins1b['id']}/report", {"number": 5})
    check(result["result"] == "confirmed", "重启后按现场新号 5 确认", str(result))

    print("5) 走完剩余 5 步（最少 6 步），每步先错号再新号")
    steps_done = 1
    while True:
        status, body = post_json(f"/clerk/plans/{plan_id}/instructions", {})
        if body.get("instruction") is None:
            break
        ins = body["instruction"]
        # 故意先填一个错号
        wrong = ins["to_number"] + 100
        status, bad = post_json(f"/clerk/instructions/{ins['id']}/report", {"number": wrong})
        check(bad["result"] == "mismatch", f"第 {ins['step_index'] + 1} 步错号被拒", str(bad))
        status, ok = post_json(f"/clerk/instructions/{ins['id']}/report", {"number": ins["to_number"]})
        check(ok["result"] == "confirmed", f"第 {ins['step_index'] + 1} 步确认", str(ok))
        steps_done += 1
        if steps_done > 12:
            check(False, "步数异常，疑似死循环")
            break
    check(steps_done == 6, "全程共 6 次换牌，达最少步数", f"实际 {steps_done}")

    status, body = post_json(f"/clerk/plans/{plan_id}/instructions", {})
    check(body.get("instruction") is None, "完成后不再发出指令", str(body))

    html = request("/clerk")[2]
    check("临时号 5 已腾出" in html or "已完成" in html, "工作台显示方案完成")

    print("6) 游客按最终牌号查讲解")
    expected = {2: "青铜鼎", 1: "青瓷瓶", 4: "玉璧", 3: "木雕佛像", 5: "临时空号"}
    for number, keyword in expected.items():
        html = request(f"/gallery/lookup?number={number}&plan={plan_id}")[2]
        check(keyword in html, f"{number} 号 -> {keyword}")

    print("7) 已完成指令重复上报：状态不变")
    status, again = post_json(f"/clerk/instructions/{ins1b['id']}/report", {"number": 5})
    check(again["result"] == "already_finalized" and not again.get("changed", False),
          "重复上报已确认指令不改变状态", str(again))
    html = request(f"/gallery/lookup?number=2&plan={plan_id}")[2]
    check("青铜鼎" in html, "重复上报后查号结果不变")

    print()
    if _failures:
        print(f"失败 {len(_failures)} / {_checks}：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{_checks} 项检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
