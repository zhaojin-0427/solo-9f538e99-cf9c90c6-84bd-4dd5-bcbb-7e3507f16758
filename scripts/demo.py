#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行演示：不依赖 Flask，跑通“建立任务 -> 载入旧档 -> 补问 -> 判定”全流程。

用法：
  python3 scripts/demo.py sample/config_with_archive.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import (
    analyze,
    build_config,
    replace_followups,
    validate_archive,
)


def show(cfg, answers, title):
    print("=" * 64)
    print(title)
    print("-" * 64)
    r = analyze(cfg, answers)
    print("已答:", {cfg.labels.get(k, k): ("是" if v else "否") for k, v in answers.items()})
    if r.verdict == "conflict":
        print("结论: 旧档矛盾，最小矛盾核：")
        for c in r.conflicting:
            print("  -", c.description)
        return r
    print(f"可行补全数: {r.feasible_count}")
    if r.implied:
        print("隐含确定:", {cfg.labels.get(k, k): ("是" if v else "否")
                            for k, v in r.implied.items()})
    if r.verdict == "uncertain":
        print(f"结论: 有分歧；从当前起最坏还要问 {r.worst_remaining} 题")
        print("候选排序:", [(cfg.labels.get(n, n), d) for n, d in r.candidates])
        print("推荐补问:", cfg.labels.get(r.recommended, r.recommended))
    else:
        verdict = "纳入新版夜骑分析" if r.verdict == "include" else "不纳入"
        print(f"结论: 直接判定 —— {verdict}（最坏剩余 0 题）")
    return r


def main(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if "config" in payload:
        config_raw, archive_raw = payload["config"], payload.get("archive")
    else:
        config_raw, archive_raw = payload, None

    cfg0 = build_config(config_raw)
    answers = {}
    cfg = cfg0
    if archive_raw is not None:
        answers, rebuilt = validate_archive(archive_raw, cfg0)
        cfg = replace_followups(cfg0, rebuilt)

    show(cfg, answers, "载入旧答卷后")

    if not answers.get("S3") and "S3" in cfg.items:
        r = analyze(cfg, answers)
        if r.satisfiable and r.verdict == "uncertain":
            trial = dict(answers)
            trial["S3"] = True
            if analyze(cfg, trial).satisfiable:
                answers = trial
                show(cfg, answers, "电话补问 S3 = 是 后")

    # 演示冲突补问：尝试给被隐含为假的项答“是”
    r = analyze(cfg, answers)
    if r.implied:
        name = next(iter(r.implied))
        trial = dict(answers)
        trial[name] = not r.implied[name]
        show(cfg, trial, f"冲突演示：{cfg.labels.get(name, name)} 答 {'是' if trial[name] else '否'}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "sample/config_with_archive.json"
    main(target)
