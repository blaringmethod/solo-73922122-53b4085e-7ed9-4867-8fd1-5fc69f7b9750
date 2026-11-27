"""换牌路径规划：给定各展品的现号与目标号，另有一个空闲临时号。

换牌规则
--------
墙上共有 n + 1 个号码：n 件展品各占一个现号，另有 1 个空闲的临时号。
每步只能把某一件展品移到当前空着的那个号码上；它腾出的号码随即成为新的空号。
目标是把每件展品都换到目标号，并且最终空着的仍必须是原来的临时号
（临时号不得用作任何展品的目标号）。

最少步数
--------
沿「展品目标号 -> 此刻占着该号的展品」追踪，可把待换的展品分成若干循环。
长度为 k >= 2 的循环必须整体轮换一次，而每轮至少 k + 1 步：
先把一件展品挪到临时号、k 次归位、最后让临时号重新空出。
不同循环互不干扰，故最少步数 = 各非平凡循环 (k + 1) 之和。
本模块只生成「展品 -> 当前空号」的合法移动，可构造达到该下界，
已对 n <= 5 的全部排列用 BFS 穷举验证与最短路径一致。
"""

from __future__ import annotations

from dataclasses import dataclass


class PlanError(ValueError):
    """录入数据不满足换牌前提。"""


@dataclass(frozen=True)
class SwapStep:
    """一步换牌：把展品从 from_number 移到 to_number（to_number 执行时为空号）。"""

    exhibit_index: int  # 展品在输入列表中的下标
    from_number: int
    to_number: int


def validate_inputs(
    current_numbers: list[int],
    target_numbers: list[int],
    temp_number: int,
) -> None:
    """校验号码设置；不合法时抛出 PlanError。"""
    if not 1 <= len(current_numbers) <= 12:
        raise PlanError("展品数量须在 1 到 12 件之间。")
    if len(current_numbers) != len(target_numbers):
        raise PlanError("现号与目标号的数量必须一致。")
    if len(set(current_numbers)) != len(current_numbers):
        raise PlanError("现号有重复，每个现号只能由一件展品使用。")
    if len(set(target_numbers)) != len(target_numbers):
        raise PlanError("目标号有重复，每个目标号只能由一件展品使用。")
    if set(current_numbers) != set(target_numbers):
        missing = sorted(set(current_numbers) - set(target_numbers))
        extra = sorted(set(target_numbers) - set(current_numbers))
        parts = []
        if missing:
            parts.append("只在现号中出现：" + "、".join(map(str, missing)))
        if extra:
            parts.append("只在目标号中出现：" + "、".join(map(str, extra)))
        raise PlanError("现号与目标号必须是同一组号码（" + "；".join(parts) + "）。")
    if temp_number in current_numbers:
        raise PlanError("临时号必须空闲，不能是任何展品的现号。")
    if temp_number in target_numbers:
        raise PlanError("临时号不得作为任何展品的目标号。")


def _cycles(
    current_numbers: list[int], target_numbers: list[int]
) -> list[list[int]]:
    """把展品按置换拆成循环，每个循环内的次序满足：

    target[c[m]] == current[c[(m + 1) % k]]

    长度 1 的循环表示该展品已在目标号上，直接略去。
    """
    n = len(current_numbers)
    holder_of = {num: i for i, num in enumerate(current_numbers)}
    # next_exhibit(i)：占着展品 i 目标号的那件展品。
    next_exhibit = [holder_of[target_numbers[i]] for i in range(n)]

    visited = [False] * n
    cycles: list[list[int]] = []
    for start in range(n):
        if visited[start]:
            continue
        cycle: list[int] = []
        cur = start
        while not visited[cur]:
            visited[cur] = True
            cycle.append(cur)
            cur = next_exhibit[cur]
        if len(cycle) > 1:
            cycles.append(cycle)
    return cycles


def plan_swaps(
    current_numbers: list[int],
    target_numbers: list[int],
    temp_number: int,
) -> list[SwapStep]:
    """返回最少步数的换牌指令序列，逐步可直接执行。"""
    validate_inputs(current_numbers, target_numbers, temp_number)
    n = len(current_numbers)
    target = {i: target_numbers[i] for i in range(n)}
    where = {i: current_numbers[i] for i in range(n)}  # 展品当前所在号
    empty = temp_number
    steps: list[SwapStep] = []

    def move(exhibit_index: int) -> None:
        nonlocal empty
        src = where[exhibit_index]
        assert src != empty, "展品已在空号上，不合逻辑"
        steps.append(SwapStep(exhibit_index, src, empty))
        where[exhibit_index] = empty
        empty = src

    for cycle in _cycles(current_numbers, target_numbers):
        # 先把循环头搬到临时号，腾出它的号码；
        # 随后沿循环逆序把每件展品搬到刚空出的目标号上，
        # 循环头最后搬回空位，临时号重新空出。
        move(cycle[0])
        remaining = set(cycle)
        while remaining:
            mover = next(i for i in remaining if target[i] == empty)
            remaining.discard(mover)
            move(mover)

    return steps


def minimal_step_count(
    current_numbers: list[int],
    target_numbers: list[int],
    temp_number: int,
) -> int:
    """最少换牌次数（公式版，便于独立核对）。"""
    validate_inputs(current_numbers, target_numbers, temp_number)
    return sum(len(c) + 1 for c in _cycles(current_numbers, target_numbers))
