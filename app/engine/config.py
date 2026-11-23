# -*- coding: utf-8 -*-
"""核查配置与旧答卷数据模型。

- AnalysisConfig：一次核查任务的静态配置（筛查项、旧版追问题展示条件、新版纳入条件）
- Archive：旧答卷上传内容（已答筛查项 + 当时实际展示/跳过的追问题号）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .conditions import ConditionError, ast_to_text, parse_condition

MAX_ITEMS = 12


@dataclass(frozen=True)
class FollowUp:
    qid: str                 # 追问题号
    condition_text: str      # 原始条件文本
    condition_ast: tuple     # 解析后的 AST
    shown: bool              # 旧答卷中该题是否实际展示过

    @property
    def condition_display(self) -> str:
        return ast_to_text(self.condition_ast)


@dataclass(frozen=True)
class AnalysisConfig:
    items: List[str]                       # 筛查项变量名，顺序即位掩码位序
    labels: Dict[str, str]                 # 变量名 -> 中文说明
    followups: List[FollowUp]
    inclusion_text: str
    inclusion_ast: tuple

    def index_of(self) -> Dict[str, int]:
        return {name: i for i, name in enumerate(self.items)}


class ConfigError(ValueError):
    """配置或答卷数据不合法。"""


def _norm_key(value) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"筛查项变量名必须是字符串，收到 {value!r}")
    key = value.strip()
    if not key:
        raise ConfigError("筛查项变量名不能为空")
    return key


def build_config(raw: dict) -> AnalysisConfig:
    """从可 JSON 序列化的 dict 构建并完整校验配置。

    raw 结构：
    {
      "items": ["S1", ...],              # 至多 12 个
      "labels": {"S1": "夜间佩戴头盔"},   # 可选
      "followups": [
         {"qid": "Q1", "condition": "S1 AND NOT S2", "shown": true}, ...
      ],
      "inclusion": "S1 AND (S2 OR S3)"
    }
    """
    if not isinstance(raw, dict):
        raise ConfigError("配置必须是 JSON 对象")

    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ConfigError("items 必须是非空数组")
    items: List[str] = []
    for it in raw_items:
        key = _norm_key(it)
        if key in items:
            raise ConfigError(f"筛查项 {key} 重复定义")
        items.append(key)
    if len(items) > MAX_ITEMS:
        raise ConfigError(f"筛查项最多 {MAX_ITEMS} 个，当前 {len(items)} 个")

    labels_raw = raw.get("labels", {}) or {}
    if not isinstance(labels_raw, dict):
        raise ConfigError("labels 必须是对象")
    labels: Dict[str, str] = {}
    for k, v in labels_raw.items():
        key = _norm_key(k)
        if key not in items:
            raise ConfigError(f"labels 中的 {key} 不在 items 列表内")
        labels[key] = str(v)
    for name in items:
        labels.setdefault(name, name)

    followups_raw = raw.get("followups", [])
    if not isinstance(followups_raw, list):
        raise ConfigError("followups 必须是数组")
    followups: List[FollowUp] = []
    seen_qids: set = set()
    for f in followups_raw:
        if not isinstance(f, dict):
            raise ConfigError("followups 每一项必须是对象")
        qid = str(f.get("qid", "")).strip()
        if not qid:
            raise ConfigError("追问题号 qid 不能为空")
        if qid in seen_qids:
            raise ConfigError(f"追问题号 {qid} 重复")
        seen_qids.add(qid)
        cond_text = f.get("condition", "")
        if not isinstance(cond_text, str) or not cond_text.strip():
            raise ConfigError(f"追问题 {qid} 的展示条件为空")
        try:
            ast = parse_condition(cond_text, items)
        except ConditionError as exc:
            raise ConfigError(f"追问题 {qid} 条件错误：{exc}") from exc
        shown = _as_bool(f.get("shown"), f"追问题 {qid} 的 shown")
        followups.append(FollowUp(qid, cond_text.strip(), ast, shown))

    inclusion_text = raw.get("inclusion", "")
    if not isinstance(inclusion_text, str) or not inclusion_text.strip():
        raise ConfigError("新版纳入条件 inclusion 为空")
    try:
        inclusion_ast = parse_condition(inclusion_text, items)
    except ConditionError as exc:
        raise ConfigError(f"新版纳入条件错误：{exc}") from exc

    cfg = AnalysisConfig(
        items=items,
        labels=labels,
        followups=followups,
        inclusion_text=inclusion_text.strip(),
        inclusion_ast=inclusion_ast,
    )
    return cfg


def _as_bool(value, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "真", "是"):
            return True
        if low in ("false", "0", "no", "假", "否"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ConfigError(f"{where} 必须是布尔值（true/false）")


def normalize_answers(raw: dict, items: List[str]) -> Dict[str, bool]:
    """校验已答筛查项：只允许配置中的变量，值必须可解释为布尔。"""
    if not isinstance(raw, dict):
        raise ConfigError("已答筛查项 answers 必须是对象")
    out: Dict[str, bool] = {}
    for k, v in raw.items():
        key = _norm_key(k)
        if key not in items:
            raise ConfigError(f"答案中的筛查项 {key} 不在 items 列表内")
        out[key] = _as_bool(v, f"筛查项 {key} 的答案")
    return out


def validate_archive(raw: dict, cfg: AnalysisConfig) -> tuple:
    """校验上传的旧答卷，返回 (归一化答案, 按答卷展示状态重建的追问题列表)。

    要求答卷覆盖全部追问题（展示或跳过都要列出，未列出即视为缺少记录），
    以避免“没列出”与“明确跳过”之间的歧义。
    """
    if not isinstance(raw, dict):
        raise ConfigError("答卷必须是 JSON 对象")
    answers = normalize_answers(raw.get("answers", {}), cfg.items)

    listed = raw.get("followups")
    if listed is None:
        # 兼容只给 shown_qids 的上传形式
        shown_qids = raw.get("shown_qids", [])
        if not isinstance(shown_qids, list):
            raise ConfigError("shown_qids 必须是数组")
        shown_set = {str(q).strip() for q in shown_qids}
        listed = [
            {"qid": f.qid, "shown": f.qid in shown_set}
            for f in cfg.followups
        ]
    if not isinstance(listed, list):
        raise ConfigError("followups 必须是数组")

    qid_to_shown = {f.qid: f.shown for f in cfg.followups}
    seen: set = set()
    for entry in listed:
        if not isinstance(entry, dict) or "qid" not in entry:
            raise ConfigError("followups 每一项必须含 qid")
        qid = str(entry["qid"]).strip()
        if qid not in qid_to_shown:
            raise ConfigError(f"答卷中的追问题号 {qid} 不在配置内")
        if qid in seen:
            raise ConfigError(f"答卷中追问题号 {qid} 重复")
        seen.add(qid)
        qid_to_shown[qid] = _as_bool(entry.get("shown", True), f"追问题 {qid} 的 shown")
    missing = set(qid_to_shown) - seen
    if missing:
        raise ConfigError(
            "答卷缺少以下追问题的展示/跳过记录：" + "、".join(sorted(missing))
        )

    # 用校验后的展示状态重建 followups（答卷数据为准）
    rebuilt = [
        FollowUp(f.qid, f.condition_text, f.condition_ast, qid_to_shown[f.qid])
        for f in cfg.followups
    ]
    return answers, rebuilt


def replace_followups(cfg: AnalysisConfig, followups: List[FollowUp]) -> AnalysisConfig:
    return AnalysisConfig(
        items=cfg.items,
        labels=cfg.labels,
        followups=followups,
        inclusion_text=cfg.inclusion_text,
        inclusion_ast=cfg.inclusion_ast,
    )


def config_to_jsonable(cfg: AnalysisConfig) -> dict:
    return {
        "items": list(cfg.items),
        "labels": dict(cfg.labels),
        "followups": [
            {"qid": f.qid, "condition": f.condition_text, "shown": f.shown}
            for f in cfg.followups
        ],
        "inclusion": cfg.inclusion_text,
    }
