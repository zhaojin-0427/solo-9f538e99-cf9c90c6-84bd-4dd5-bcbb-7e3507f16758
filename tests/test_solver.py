# -*- coding: utf-8 -*-
"""判定引擎测试：跳题约束、矛盾核、最坏提问数 minimax。"""
import unittest

from app.engine import (
    analyze,
    build_config,
    replace_followups,
    validate_archive,
)


def make(cfg_raw, archive_raw=None):
    cfg0 = build_config(cfg_raw)
    if archive_raw is None:
        return cfg0, {}
    answers, rebuilt = validate_archive(archive_raw, cfg0)
    return replace_followups(cfg0, rebuilt), answers


BASE_CFG = {
    "items": ["S1", "S2", "S3"],
    "labels": {"S1": "头盔", "S2": "车灯", "S3": "超30分钟"},
    "followups": [
        {"qid": "Q1", "condition": "S1 AND NOT S2", "shown": True},
        {"qid": "Q2", "condition": "S3 OR S1", "shown": False},
    ],
    "inclusion": "S1 AND (S2 OR S3)",
}


class FeasibilityTests(unittest.TestCase):
    def test_shown_followup_requires_true_condition(self):
        # Q1 展示 => S1=真, S2=假；Q2 跳过 => S3=假 且 S1=假，与 S1=真矛盾
        cfg, answers = make(
            BASE_CFG,
            {
                "answers": {"S1": True},
                "followups": [
                    {"qid": "Q1", "shown": True},
                    {"qid": "Q2", "shown": False},
                ],
            },
        )
        result = analyze(cfg, answers)
        self.assertFalse(result.satisfiable)
        self.assertEqual(result.verdict, "conflict")
        refs = {c.ref for c in result.conflicting}
        # 两条跳题记录彼此矛盾（在 S1=真的前提下），构成极小矛盾核
        self.assertEqual(refs, {"Q1", "Q2"})

    def test_skipped_followup_requires_false_condition(self):
        # Q1 跳过 => NOT(S1 AND NOT S2)；答案 S1=真 S2=假 与之冲突
        cfg, answers = make(
            BASE_CFG,
            {
                "answers": {"S1": True, "S2": False},
                "followups": [
                    {"qid": "Q1", "shown": False},
                    {"qid": "Q2", "shown": True},
                ],
            },
        )
        result = analyze(cfg, answers)
        self.assertEqual(result.verdict, "conflict")
        self.assertIn("Q1", {c.ref for c in result.conflicting})

    def test_consistent_archive_uncertain_then_resolved(self):
        # Q1 展示 => S1=1,S2=0；Q2 展示 => S3 或 S1 为真（S1 已满足）
        cfg, answers = make(
            BASE_CFG,
            {
                "answers": {"S1": True},
                "followups": [
                    {"qid": "Q1", "shown": True},
                    {"qid": "Q2", "shown": True},
                ],
            },
        )
        result = analyze(cfg, answers)
        self.assertTrue(result.satisfiable)
        # S2 被 Q1 隐含为假；S3 未定；纳入 = S1 AND (S2 OR S3) = S3
        self.assertEqual(result.implied.get("S2"), False)
        self.assertEqual(result.verdict, "uncertain")
        self.assertEqual(result.recommended, "S3")
        self.assertEqual(result.worst_remaining, 1)

        answers["S3"] = True
        result2 = analyze(cfg, answers)
        self.assertEqual(result2.verdict, "include")
        self.assertEqual(result2.worst_remaining, 0)

        answers["S3"] = False
        result3 = analyze(cfg, answers)
        self.assertEqual(result3.verdict, "exclude")

    def test_archive_missing_followup_record_rejected(self):
        cfg0 = build_config(BASE_CFG)
        with self.assertRaises(Exception):
            validate_archive(
                {"answers": {}, "followups": [{"qid": "Q1", "shown": True}]},
                cfg0,
            )

    def test_unknown_followup_qid_rejected(self):
        cfg0 = build_config(BASE_CFG)
        with self.assertRaises(Exception):
            validate_archive(
                {
                    "answers": {},
                    "followups": [
                        {"qid": "Q1", "shown": True},
                        {"qid": "Q2", "shown": False},
                        {"qid": "QX", "shown": True},
                    ],
                },
                cfg0,
            )


class MinimaxDepthTests(unittest.TestCase):
    def test_independent_vars_worst_case_n(self):
        for n in range(1, 5):
            cfg = build_config(
                {
                    "items": [f"S{i}" for i in range(1, n + 1)],
                    "followups": [],
                    "inclusion": " AND ".join(f"S{i}" for i in range(1, n + 1)),
                }
            )
            result = analyze(cfg, {})
            self.assertEqual(result.worst_remaining, n)
            self.assertIsNotNone(result.recommended)

    def test_or_formula_depth(self):
        # S1 OR S2：先问一题，答否才需要第二题 => 最坏 2
        cfg = build_config(
            {"items": ["S1", "S2"], "followups": [], "inclusion": "S1 OR S2"}
        )
        result = analyze(cfg, {})
        self.assertEqual(result.worst_remaining, 2)

    def test_irrelevant_variable_not_counted(self):
        # 纳入条件只依赖 S1；S2、S3 无关，最坏 1 题且推荐 S1
        cfg = build_config(
            {
                "items": ["S1", "S2", "S3"],
                "followups": [],
                "inclusion": "S1",
            }
        )
        result = analyze(cfg, {})
        self.assertEqual(result.worst_remaining, 1)
        self.assertEqual(result.recommended, "S1")
        cand = dict(result.candidates)
        self.assertEqual(cand["S1"], 1)
        # 问无关变量不能消除分歧，故其最坏题数为 2
        self.assertEqual(cand["S2"], 2)

    def test_constraint_can_make_depth_zero(self):
        # 纳入条件 S1 AND S2；约束把 S1、S2 都隐含为真 => 直接判定
        cfg = build_config(
            {
                "items": ["S1", "S2"],
                "followups": [
                    {"qid": "Q1", "condition": "S1 AND S2", "shown": True}
                ],
                "inclusion": "S1 AND S2",
            }
        )
        result = analyze(cfg, {})
        self.assertEqual(result.verdict, "include")
        self.assertEqual(result.worst_remaining, 0)

    def test_conflicting_answer_trial(self):
        # 已答 S1=真；再尝试 S1=假在路由层被拒，这里直接验证引擎层面约束冲突
        cfg = build_config(
            {
                "items": ["S1"],
                "followups": [{"qid": "Q1", "condition": "S1", "shown": True}],
                "inclusion": "S1",
            }
        )
        ok = analyze(cfg, {"S1": True})
        bad = analyze(cfg, {"S1": False})
        self.assertEqual(ok.verdict, "include")
        self.assertEqual(bad.verdict, "conflict")
        self.assertIn("Q1", {c.ref for c in bad.conflicting})


class ConfigValidationTests(unittest.TestCase):
    def test_max_items_enforced(self):
        raw = {
            "items": [f"S{i}" for i in range(13)],
            "followups": [],
            "inclusion": "S1",
        }
        with self.assertRaises(Exception):
            build_config(raw)

    def test_duplicate_items_rejected(self):
        with self.assertRaises(Exception):
            build_config(
                {"items": ["S1", "S1"], "followups": [], "inclusion": "S1"}
            )

    def test_bad_inclusion_syntax_rejected(self):
        with self.assertRaises(Exception):
            build_config(
                {"items": ["S1"], "followups": [], "inclusion": "S1 AND"}
            )


if __name__ == "__main__":
    unittest.main()
