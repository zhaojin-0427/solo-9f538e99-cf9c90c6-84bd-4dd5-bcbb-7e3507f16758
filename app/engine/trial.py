# -*- coding: utf-8 -*-
"""试跑核查引擎：带版号的问卷配置、试跑记录规范化与规则预期。

与旧档纳入核查不同，这里不做补全枚举，只回答一个确定的问题：
在某个版号、某一组完整筛查答案下，某个追问题按该版规则**本应展示还是跳过**。

数据模型：
- TrialVersion：版号配置（筛查项、追问题及其展示条件）
- TrialRecord：一行试跑记录（试跑号、终端号、版号、完整筛查答案、
  逐题展示/跳过记录）

JSONL 每行一个 TrialRecord。记录规范化后会得到稳定的 content_hash，
用于“同试跑号重传相同内容不增计、异内容必须更正”的判定。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .conditions import ConditionError, evaluate, parse_condition


class TrialError(ValueError):
    """试跑配置或试跑记录不合法。"""


@dataclass(frozen=True)
class TrialFollowup:
    qid: str
    condition_text: str
    condition_ast: tuple


@dataclass(frozen=True)
class TrialVersion:
    version: str
    items: Tuple[str, ...]
    labels: Dict[str, str]
    followups: Tuple[TrialFollowup, ...]
    config_json: str

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.config_json.encode("utf-8")).hexdigest()

    @property
    def qids(self) -> Tuple[str, ...]:
        return tuple(f.qid for f in self.followups)

    def followup_map(self) -> Dict[str, TrialFollowup]:
        return {f.qid: f for f in self.followups}

    def expected_shown(self, qid: str, answers: Dict[str, bool]) -> bool:
        followup = self.followup_map().get(qid)
        if followup is None:
            raise TrialError(f"版号 {self.version} 没有追问题 {qid}")
        return bool(evaluate(followup.condition_ast, answers))


@dataclass(frozen=True)
class TrialRecord:
    trial_id: str
    version: str
    terminal: str
    answers: Dict[str, bool]
    followups: Tuple[Tuple[str, bool], ...]   # 按配置中的追问题顺序
    content_hash: str
    answers_json: str
    followups_json: str

    @property
    def qids(self) -> Tuple[str, ...]:
        return tuple(qid for qid, _ in self.followups)

    def shown_of(self, qid: str) -> bool:
        for name, shown in self.followups:
            if name == qid:
                return shown
        raise TrialError(f"试跑 {self.trial_id} 没有追问题 {qid} 的记录")


def _canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise TrialError(f"{where} 必须是字符串")
    text = value.strip()
    if not text:
        raise TrialError(f"{where} 不能为空")
    return text


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "真", "是"):
            return True
        if low in ("false", "0", "no", "假", "否"):
            return False
    raise TrialError(f"{where} 必须是布尔值（true/false）")


def _as_shown(value: Any, where: str) -> bool:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("shown", "show", "displayed", "display", "展示", "显示"):
            return True
        if low in ("skipped", "skip", "hidden", "跳过", "未展示"):
            return False
    return _as_bool(value, where)


def _pick_alias(raw: Dict[str, Any], names: Sequence[str], where: str) -> Any:
    """取多个别名中的同一个字段；同时出现且值不同则报错避免歧义。"""
    present = [(name, raw[name]) for name in names if name in raw]
    if not present:
        raise TrialError(f"缺少{where}（可用字段：{'、'.join(names)}）")
    first_name, first_value = present[0]
    for name, value in present[1:]:
        if value != first_value:
            raise TrialError(
                f"{where}存在冲突的重复字段 {first_name} / {name}"
            )
    return first_value


def _present_alias(raw: Dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


TRIAL_ID_FIELDS = ("trial_id", "trial_no", "run_id", "试跑号")
TERMINAL_FIELDS = ("terminal", "terminal_id", "device_id", "终端号")
VERSION_FIELDS = ("version", "version_id", "版号")
ANSWERS_FIELDS = ("answers", "screening_answers", "筛查答案")
FOLLOWUP_FIELDS = (
    "followups",
    "records",
    "followup_records",
    "展示跳过记录",
    "跳题记录",
)


# ---------------------------------------------------------------------------
# 版号配置
# ---------------------------------------------------------------------------

def parse_version_configs(raw: Any) -> List[TrialVersion]:
    """解析一份或多份版号配置。

    支持：
    - 单版号对象：{"version": "v1", "items": [...], "followups": [...]}
    - 多版号：{"versions": [ ... ]} 或 {"versions": {"v1": {...}}}
    - 版号映射：{"v1": {...}, "v2": {...}}
    """
    entries: List[Tuple[Optional[str], Any]] = []
    if isinstance(raw, list):
        entries = [(None, item) for item in raw]
    elif isinstance(raw, dict):
        if "versions" in raw:
            bundled = raw["versions"]
            if isinstance(bundled, dict):
                entries = [(str(k).strip(), v) for k, v in bundled.items()]
            elif isinstance(bundled, list):
                entries = [(None, item) for item in bundled]
            else:
                raise TrialError("versions 必须是数组或对象")
        elif "version" in raw:
            entries = [(None, raw)]
        elif "items" in raw or "followups" in raw:
            raise TrialError("版号配置缺少 version（版号）字段")
        else:
            entries = [(str(k).strip(), v) for k, v in raw.items()]
    else:
        raise TrialError("版号配置必须是 JSON 对象或数组")

    if not entries:
        raise TrialError("没有可用的版号配置")

    parsed: List[TrialVersion] = []
    by_version: Dict[str, TrialVersion] = {}
    for key, entry in entries:
        if not isinstance(entry, dict):
            raise TrialError(f"版号 {key or '（未命名）'} 的配置必须是 JSON 对象")
        explicit = entry.get("version", key)
        if explicit is None:
            raise TrialError("每个版号配置都必须带 version（版号）")
        version = _text(explicit, "版号")
        if key is not None and key and key != version:
            raise TrialError(
                f"版号映射键 {key!r} 与配置内的 version {version!r} 不一致"
            )
        cfg = _parse_one_version(version, entry)
        if version in by_version:
            if by_version[version].config_json != cfg.config_json:
                raise TrialError(f"版号 {version} 在配置中重复且内容不同")
            continue
        by_version[version] = cfg
        parsed.append(cfg)
    return parsed


def _parse_one_version(version: str, raw: Dict[str, Any]) -> TrialVersion:
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise TrialError(f"版号 {version} 的 items 必须是非空数组")
    items: List[str] = []
    for value in items_raw:
        item = _text(value, f"版号 {version} 的筛查项")
        if item in items:
            raise TrialError(f"版号 {version} 的筛查项 {item} 重复")
        items.append(item)

    labels_raw = raw.get("labels", {}) or {}
    if not isinstance(labels_raw, dict):
        raise TrialError(f"版号 {version} 的 labels 必须是对象")
    labels: Dict[str, str] = {}
    for key, value in labels_raw.items():
        name = _text(key, f"版号 {version} 的 labels 键")
        if name not in items:
            raise TrialError(f"版号 {version} 的 labels 中 {name} 不在 items 内")
        labels[name] = str(value)
    for name in items:
        labels.setdefault(name, name)

    followups_raw = raw.get("followups", [])
    if isinstance(followups_raw, dict):
        followups_raw = [
            {"qid": qid, "condition": cond}
            for qid, cond in followups_raw.items()
        ]
    if not isinstance(followups_raw, list):
        raise TrialError(f"版号 {version} 的 followups 必须是数组或对象")

    followups: List[TrialFollowup] = []
    seen: set = set()
    for entry in followups_raw:
        if not isinstance(entry, dict):
            raise TrialError(f"版号 {version} 的 followups 每一项必须是对象")
        qid = _text(entry.get("qid", ""), f"版号 {version} 的追问题号")
        if qid in seen:
            raise TrialError(f"版号 {version} 的追问题号 {qid} 重复")
        seen.add(qid)
        cond_text = entry.get("condition", "")
        if not isinstance(cond_text, str) or not cond_text.strip():
            raise TrialError(f"版号 {version} 的追问题 {qid} 展示条件为空")
        try:
            ast = parse_condition(cond_text, items)
        except ConditionError as exc:
            raise TrialError(
                f"版号 {version} 的追问题 {qid} 条件错误：{exc}"
            ) from exc
        followups.append(TrialFollowup(qid, cond_text.strip(), ast))

    config_payload = {
        "version": version,
        "items": items,
        "labels": labels,
        "followups": [
            {"qid": f.qid, "condition": f.condition_text} for f in followups
        ],
    }
    return TrialVersion(
        version=version,
        items=tuple(items),
        labels=labels,
        followups=tuple(followups),
        config_json=_canonical_json(config_payload),
    )


def version_config_to_jsonable(cfg: TrialVersion) -> dict:
    return json.loads(cfg.config_json)


# ---------------------------------------------------------------------------
# 试跑记录
# ---------------------------------------------------------------------------

def record_version(raw: Dict[str, Any]) -> str:
    """只提取版号，供上传时先选配置再完整规范化。"""
    if not isinstance(raw, dict):
        raise TrialError("试跑记录必须是 JSON 对象")
    return _text(_pick_alias(raw, VERSION_FIELDS, "版号"), "版号")


def normalize_trial_record(raw: Dict[str, Any], cfg: TrialVersion) -> TrialRecord:
    """把一行 JSONL 规范化并校验完整性。"""
    if not isinstance(raw, dict):
        raise TrialError("试跑记录必须是 JSON 对象")
    trial_id = _text(_pick_alias(raw, TRIAL_ID_FIELDS, "试跑号"), "试跑号")
    terminal = _text(_pick_alias(raw, TERMINAL_FIELDS, "终端号"), "终端号")
    version = _text(_pick_alias(raw, VERSION_FIELDS, "版号"), "版号")
    if version != cfg.version:
        raise TrialError(
            f"试跑 {trial_id} 的版号 {version} 与所选配置 {cfg.version} 不一致"
        )

    answers_raw = _pick_alias(raw, ANSWERS_FIELDS, "完整筛查答案")
    if not isinstance(answers_raw, dict):
        raise TrialError(f"试跑 {trial_id} 的 answers 必须是对象")
    extra = set(answers_raw) - set(cfg.items)
    missing = set(cfg.items) - set(answers_raw)
    if extra:
        raise TrialError(
            f"试跑 {trial_id} 的答案含配置外筛查项：" + "、".join(sorted(extra))
        )
    if missing:
        raise TrialError(
            f"试跑 {trial_id} 缺少完整筛查答案：" + "、".join(sorted(missing))
        )
    answers: Dict[str, bool] = {}
    for item in cfg.items:
        answers[item] = _as_bool(
            answers_raw[item], f"试跑 {trial_id} 的筛查项 {item} 答案"
        )

    followups_raw = _pick_alias(raw, FOLLOWUP_FIELDS, "逐题展示/跳过记录")
    shown_by_qid = _parse_followup_records(followups_raw, trial_id)
    cfg_qids = cfg.qids
    extra_q = set(shown_by_qid) - set(cfg_qids)
    missing_q = set(cfg_qids) - set(shown_by_qid)
    if extra_q:
        raise TrialError(
            f"试跑 {trial_id} 含版号 {version} 未配置的追问题号："
            + "、".join(sorted(extra_q))
        )
    if missing_q:
        raise TrialError(
            f"试跑 {trial_id} 缺少以下追问题的展示/跳过记录："
            + "、".join(sorted(missing_q))
        )
    followups = tuple((qid, shown_by_qid[qid]) for qid in cfg_qids)

    canonical = {
        "version": version,
        "terminal": terminal,
        "answers": {item: answers[item] for item in cfg.items},
        "followups": [
            {"qid": qid, "shown": shown} for qid, shown in followups
        ],
    }
    answers_json = _canonical_json(answers)
    followups_json = _canonical_json(canonical["followups"])
    content_hash = hashlib.sha256(
        _canonical_json(canonical).encode("utf-8")
    ).hexdigest()
    return TrialRecord(
        trial_id=trial_id,
        version=version,
        terminal=terminal,
        answers=answers,
        followups=followups,
        content_hash=content_hash,
        answers_json=answers_json,
        followups_json=followups_json,
    )


def _parse_followup_records(raw: Any, trial_id: str) -> Dict[str, bool]:
    if isinstance(raw, dict):
        entries: Iterable[Any] = [
            {"qid": qid, "shown": value} for qid, value in raw.items()
        ]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise TrialError(
            f"试跑 {trial_id} 的逐题记录必须是数组或对象"
        )

    out: Dict[str, bool] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TrialError(f"试跑 {trial_id} 的逐题记录每一项必须是对象")
        qid = _text(entry.get("qid", ""), f"试跑 {trial_id} 的追问题号")
        if qid in out:
            raise TrialError(f"试跑 {trial_id} 的追问题号 {qid} 重复记录")
        if "shown" in entry:
            value = entry["shown"]
        elif "action" in entry:
            value = entry["action"]
        elif "displayed" in entry:
            value = entry["displayed"]
        elif "展示" in entry:
            value = entry["展示"]
        else:
            raise TrialError(
                f"试跑 {trial_id} 的追问题 {qid} 缺少 shown（展示/跳过）"
            )
        out[qid] = _as_shown(value, f"试跑 {trial_id} 的追问题 {qid} 记录")
    return out


def trial_record_to_json(record: Dict[str, Any]) -> str:
    """把存储层返回的试跑记录渲染为可编辑的 JSON 文本。"""
    payload = {
        "trial_id": record["trial_id"],
        "terminal": record["terminal"],
        "version": record["version"],
        "answers": record["answers"],
        "followups": record["followups"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def validate_answers_against_version(
    answers: Dict[str, bool], cfg: TrialVersion
) -> None:
    if set(answers) != set(cfg.items):
        raise TrialError(
            f"同答案组的筛查项集合与版号 {cfg.version} 的 items 不一致"
        )
