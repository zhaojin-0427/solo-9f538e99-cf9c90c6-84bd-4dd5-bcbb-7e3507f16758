# -*- coding: utf-8 -*-
"""判定引擎：可行性枚举 + 最小最坏提问数。

把 n（<=12）个是非筛查项的取值组合编码为 n 位掩码，逐一枚举全部 2^n 种补全，
再用旧答卷约束（展示/跳过的追问题条件）与已答筛查项过滤：

- 某追问题“展示过” => 其展示条件在补全下必须为真
- 某追问题“跳过”   => 其展示条件在补全下必须为假
- 已答筛查项        => 补全必须与答案一致

过滤后：
- 无可行补全            => 旧档矛盾，给出最小矛盾约束核
- 全部可行补全结论一致  => 直接判定纳入/不纳入
- 结论仍有分歧          => 用 minimax 选出使“从当前起最坏还要问几题”最小的未答项
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .conditions import evaluate_mask
from .config import AnalysisConfig, FollowUp


@dataclass(frozen=True)
class Constraint:
    kind: str          # "answer" | "followup"
    ref: str           # 筛查项变量名 或 追问题号
    required: bool     # 条件必须取的布尔值
    description: str   # 面向页面的中文描述


@dataclass
class AnalysisResult:
    satisfiable: bool
    answers: Dict[str, bool]
    feasible_count: int = 0
    inclusion_values: Optional[set] = None          # 可行补全下纳入条件的取值集合
    verdict: Optional[str] = None                   # "include" / "exclude" / "uncertain" / "conflict"
    worst_remaining: Optional[int] = None           # 从当前状态起最坏还要问几题
    recommended: Optional[str] = None               # 推荐补问的筛查项变量名
    candidates: List[Tuple[str, int]] = field(default_factory=list)
    implied: Dict[str, bool] = field(default_factory=dict)   # 被现有约束唯一确定的未答项
    conflicting: List[Constraint] = field(default_factory=list)      # 最小矛盾核
    feasibility_by_mask: Optional[List[bool]] = None


def build_constraints(cfg: AnalysisConfig, answers: Dict[str, bool]) -> List[Constraint]:
    constraints: List[Constraint] = []
    for name, value in answers.items():
        constraints.append(
            Constraint(
                kind="answer",
                ref=name,
                required=bool(value),
                description=f"已答筛查项 {cfg.labels.get(name, name)} = {'是' if value else '否'}",
            )
        )
    for f in cfg.followups:
        verb = "展示" if f.shown else "跳过"
        constraints.append(
            Constraint(
                kind="followup",
                ref=f.qid,
                required=bool(f.shown),
                description=f"追问题 {f.qid} 当时被{verb}，条件（{f.condition_display}）须为{'真' if f.shown else '假'}",
            )
        )
    return constraints


def _mask_feasible(mask: int, cfg: AnalysisConfig, constraints: List[Constraint],
                   index_of: Dict[str, int]) -> bool:
    for c in constraints:
        if c.kind == "answer":
            actual = bool(mask & (1 << index_of[c.ref]))
        else:
            followup = _followup_map(cfg)[c.ref]
            actual = evaluate_mask(followup.condition_ast, mask, index_of)
        if actual != c.required:
            return False
    return True


def _followup_map(cfg: AnalysisConfig) -> Dict[str, FollowUp]:
    return {f.qid: f for f in cfg.followups}


def enumerate_feasible(cfg: AnalysisConfig, answers: Dict[str, bool]
                       ) -> Tuple[List[bool], List[Constraint]]:
    """返回每个掩码是否可行，以及本次使用的约束列表。"""
    n = len(cfg.items)
    index_of = cfg.index_of()
    constraints = build_constraints(cfg, answers)
    flags = [_mask_feasible(mask, cfg, constraints, index_of)
             for mask in range(1 << n)]
    return flags, constraints


def minimal_conflict(cfg: AnalysisConfig, answers: Dict[str, bool],
                     constraints: List[Constraint]) -> List[Constraint]:
    """求最小（按包含关系极小）不可满足约束核。

    思路：全集不可满足时，逐个尝试移除约束——移除后仍不可满足就永久删掉，
    剩下的每个约束都必不可少，构成一个极小核。
    """
    index_of = cfg.index_of()
    fmap = _followup_map(cfg)

    def sat(subset: List[Constraint]) -> bool:
        for mask in range(1 << len(cfg.items)):
            ok = True
            for c in subset:
                if c.kind == "answer":
                    actual = bool(mask & (1 << index_of[c.ref]))
                else:
                    actual = evaluate_mask(fmap[c.ref].condition_ast, mask, index_of)
                if actual != c.required:
                    ok = False
                    break
            if ok:
                return True
        return False

    core = list(constraints)
    for c in list(core):
        trial = [x for x in core if x is not c]
        if not sat(trial):
            core = trial
    return core


def _inclusion_values(cfg: AnalysisConfig, flags: List[bool]) -> set:
    index_of = cfg.index_of()
    values = set()
    for mask, ok in enumerate(flags):
        if ok:
            values.add(evaluate_mask(cfg.inclusion_ast, mask, index_of))
    return values


def decision_depth(cfg: AnalysisConfig, answers: Dict[str, bool],
                   flags: Optional[List[bool]] = None
                   ) -> Tuple[int, Optional[str], List[Tuple[str, int]], Dict[str, bool]]:
    """计算从当前状态出发的最小最坏提问数。

    返回 (最坏题数, 推荐变量, 全部候选变量及其最坏题数, 被约束隐含确定的未答项)。
    若纳入结论已唯一（或无未答项），最坏题数为 0。

    优化：可行补全预存为掩码元组，状态转移只在当前相容补全集上做位过滤；
    递归仅访问由可行补全实际可达的状态（n<=12 时上界 3^n 约 53 万），
    配合记忆化与“遇到理论下限 1 即剪枝”，满配也在毫秒级完成。
    """
    n = len(cfg.items)
    index_of = cfg.index_of()
    if flags is None:
        flags, _ = enumerate_feasible(cfg, answers)

    feasible_masks = tuple(mask for mask, ok in enumerate(flags) if ok)
    inclusion_of = [
        evaluate_mask(cfg.inclusion_ast, mask, index_of)
        for mask in range(1 << n)
    ]

    fixed_mask = 0
    fixed_bits = 0
    for name, value in answers.items():
        bit = 1 << index_of[name]
        fixed_bits |= bit
        if value:
            fixed_mask |= bit

    implied: Dict[str, bool] = {}
    for name in cfg.items:
        if name in answers:
            continue
        bit = 1 << index_of[name]
        vals = {bool(mask & bit) for mask in feasible_masks}
        if len(vals) == 1:
            implied[name] = vals.pop()

    initial_pool = tuple(
        m for m in feasible_masks
        if (m & fixed_bits) == (fixed_mask & fixed_bits)
    )

    memo: Dict[Tuple[int, int], int] = {}

    def depth(pool: Tuple[int, ...], cur_mask: int, cur_bits: int) -> int:
        outcomes = {inclusion_of[mask] for mask in pool}
        if len(outcomes) <= 1:
            return 0
        key = (cur_mask, cur_bits)
        cached = memo.get(key)
        if cached is not None:
            return cached
        best = n + 1
        for idx in range(n):
            bit = 1 << idx
            if cur_bits & bit:
                continue
            branch_false = tuple(m for m in pool if not (m & bit))
            branch_true = tuple(m for m in pool if m & bit)
            branches = [b for b in (branch_false, branch_true) if b]
            if not branches:
                continue
            need = 1 + max(
                depth(b, cur_mask | (bit if i else 0), cur_bits | bit)
                for i, b in enumerate(branches)
            )
            if need < best:
                best = need
                if best == 1:
                    break  # 理论下限，提前剪枝
        result = 0 if best > n else best
        memo[key] = result
        return result

    # 逐变量计算，用于页面展示候选排序
    candidates: List[Tuple[str, int]] = []
    for idx, name in enumerate(cfg.items):
        if name in answers:
            continue
        bit = 1 << idx
        branch_false = tuple(m for m in initial_pool if not (m & bit))
        branch_true = tuple(m for m in initial_pool if m & bit)
        branches = [b for b in (branch_false, branch_true) if b]
        if branches:
            candidates.append((
                name,
                1 + max(depth(b, bit if i else 0, bit) for i, b in enumerate(branches)),
            ))

    candidates.sort(key=lambda t: (t[1], cfg.items.index(t[0])))
    worst = depth(initial_pool, fixed_mask, fixed_bits)
    recommended = candidates[0][0] if candidates else None
    return worst, recommended, candidates, implied


def analyze(cfg: AnalysisConfig, answers: Dict[str, bool]) -> AnalysisResult:
    """主入口：对当前已答状态做完整判定。"""
    flags, constraints = enumerate_feasible(cfg, answers)
    feasible_count = sum(flags)

    if feasible_count == 0:
        conflict = minimal_conflict(cfg, answers, constraints)
        return AnalysisResult(
            satisfiable=False,
            answers=dict(answers),
            feasible_count=0,
            inclusion_values=set(),
            verdict="conflict",
            worst_remaining=None,
            recommended=None,
            candidates=[],
            implied={},
            conflicting=conflict,
            feasibility_by_mask=flags,
        )

    values = _inclusion_values(cfg, flags)
    if len(values) == 1:
        verdict = "include" if True in values else "exclude"
        worst, recommended, candidates, implied = decision_depth(cfg, answers, flags)
        return AnalysisResult(
            satisfiable=True,
            answers=dict(answers),
            feasible_count=feasible_count,
            inclusion_values=values,
            verdict=verdict,
            worst_remaining=0,
            recommended=None,
            candidates=[],
            implied=implied,
            conflicting=[],
            feasibility_by_mask=flags,
        )

    worst, recommended, candidates, implied = decision_depth(cfg, answers, flags)
    return AnalysisResult(
        satisfiable=True,
        answers=dict(answers),
        feasible_count=feasible_count,
        inclusion_values=values,
        verdict="uncertain",
        worst_remaining=worst,
        recommended=recommended,
        candidates=candidates,
        implied=implied,
        conflicting=[],
        feasibility_by_mask=flags,
    )
