#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行演示：不依赖 Flask，跑通“载入版号配置 -> 试跑记录 -> 异常组 -> 更正”。

用法：
  python3 scripts/trial_demo.py sample/trial_versions.json sample/trials.jsonl
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine.trial import normalize_trial_record, parse_version_configs
from app.store import TrialStore


def load(version_path, trials_path):
    with open(version_path, encoding="utf-8") as fh:
        configs = parse_version_configs(json.load(fh))
    by_version = {c.version: c for c in configs}
    records = []
    with open(trials_path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                records.append(normalize_trial_record(raw, by_version[raw["version"]]))
            except Exception as exc:  # noqa: BLE001 - 演示脚本直接给出可读错误
                raise SystemExit(f"第 {lineno} 行无效：{exc}") from exc
    return configs, records


def show(store, title, version=None, qid=None):
    print("=" * 68)
    print(title)
    print("-" * 68)
    result = store.list_anomalies(version, qid, 1, 50)
    print(f"异常组：{result['total']}")
    for row in result["rows"]:
        print(
            f"  版号 {row['version']} | {row['qid']} | "
            f"答案 {row['answers']} | 该版本应{row['expected_text']}"
        )
        for side, ev in (("展示", row["shown_evidence"]), ("跳过", row["skipped_evidence"])):
            print(f"    {side}侧证据：{ev['trial_id']}（终端 {ev['terminal']}）")
        wrong = row["mismatch_evidence"]
        print(
            f"    与预期不符：{wrong['trial_id']}（终端 {wrong['terminal']}，"
            f"实测{'跳过' if wrong is row['skipped_evidence'] else '展示'}，"
            f"规则要求{row['expected_text']}）"
        )
    return result


def main(version_path, trials_path):
    db_dir = tempfile.mkdtemp(prefix="trial-demo-")
    store = TrialStore(os.path.join(db_dir, "trials.db"))
    store.init_schema()
    configs, records = load(version_path, trials_path)
    store.upload_versions({"versions": [_config_payload(c) for c in configs]})
    outcome = store.upload_records(records)
    print(f"载入 {len(configs)} 个版号、{len(outcome['inserted'])} 条试跑记录。")
    show(store, "初次核查")

    # 更正演示：把一条与预期不符的记录改成符合规则，观察证据替换/异常撤下
    target = next(
        (r for r in records if r.trial_id == "R1002"),
        None,
    )
    if target is not None:
        fixed = dict(
            trial_id=target.trial_id,
            terminal=target.terminal,
            version=target.version,
            answers=target.answers,
            followups=[
                {"qid": qid, "shown": store.get_version(target.version)
                 .expected_shown(qid, target.answers)}
                for qid, _ in target.followups
            ],
        )
        cfg = store.get_version(target.version)
        store.correct_record(normalize_trial_record(fixed, cfg))
        print()
        show(store, f"按试跑号更正 {target.trial_id} 为符合该版规则后")


def _config_payload(cfg):
    return {
        "version": cfg.version,
        "items": list(cfg.items),
        "labels": dict(cfg.labels),
        "followups": [
            {"qid": f.qid, "condition": f.condition_text} for f in cfg.followups
        ],
    }


if __name__ == "__main__":
    vp = sys.argv[1] if len(sys.argv) > 1 else "sample/trial_versions.json"
    tp = sys.argv[2] if len(sys.argv) > 2 else "sample/trials.jsonl"
    main(vp, tp)
