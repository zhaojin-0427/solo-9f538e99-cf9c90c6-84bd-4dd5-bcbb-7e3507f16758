# -*- coding: utf-8 -*-
"""条件解析器测试。"""
import unittest

from app.engine.conditions import (
    ConditionError,
    ast_to_text,
    evaluate,
    parse_condition,
    variables_in,
)

ALLOWED = ["S1", "S2", "S3", "X_1"]


class ParserTests(unittest.TestCase):
    def test_basic_and_or_not(self):
        ast = parse_condition("S1 AND NOT S2 OR S3", ALLOWED)
        self.assertEqual(
            evaluate(ast, {"S1": True, "S2": False, "S3": False}), True
        )
        self.assertEqual(
            evaluate(ast, {"S1": True, "S2": True, "S3": False}), False
        )
        self.assertEqual(variables_in(ast), {"S1", "S2", "S3"})

    def test_precedence_and_binds_tighter_than_or(self):
        # S1 OR S2 AND S3 == S1 OR (S2 AND S3)
        ast = parse_condition("S1 OR S2 AND S3", ALLOWED)
        self.assertTrue(evaluate(ast, {"S1": True, "S2": False, "S3": False}))
        self.assertFalse(evaluate(ast, {"S1": False, "S2": True, "S3": False}))
        self.assertTrue(evaluate(ast, {"S1": False, "S2": True, "S3": True}))

    def test_parens_and_chinese_operators(self):
        ast = parse_condition("(S1 或 S2) 与 非S3", ALLOWED)
        self.assertTrue(evaluate(ast, {"S1": True, "S2": False, "S3": False}))
        self.assertFalse(evaluate(ast, {"S1": True, "S2": True, "S3": True}))

    def test_symbol_operators(self):
        ast = parse_condition("S1 && (S2 || !S3)", ALLOWED)
        self.assertTrue(evaluate(ast, {"S1": True, "S2": False, "S3": False}))
        self.assertFalse(evaluate(ast, {"S1": False, "S2": True, "S3": False}))

    def test_underscore_var_and_constants(self):
        ast = parse_condition("X_1 AND true OR false", ALLOWED)
        self.assertTrue(evaluate(ast, {"X_1": True}))
        self.assertFalse(evaluate(ast, {"X_1": False}))

    def test_unknown_var_rejected(self):
        with self.assertRaises(ConditionError):
            parse_condition("S1 AND S9", ALLOWED)

    def test_syntax_errors(self):
        for bad in ["", "S1 AND", "(S1", "S1 S2", "AND S1", "S1)", "1abc"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ConditionError):
                    parse_condition(bad, ALLOWED)

    def test_ast_roundtrip_text(self):
        ast = parse_condition("S1 AND (S2 OR NOT S3)", ALLOWED)
        text = ast_to_text(ast)
        ast2 = parse_condition(text, ALLOWED)
        assignment = {"S1": True, "S2": False, "S3": True}
        self.assertEqual(evaluate(ast, assignment), evaluate(ast2, assignment))


if __name__ == "__main__":
    unittest.main()
