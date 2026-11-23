# -*- coding: utf-8 -*-
"""布尔条件解析器。

支持至多 12 个是非筛查项（变量），条件语法：

    变量        例: S1
    括号        ( ... )
    与 / 且     AND / && / & / 与 / 且
    或          OR  / || / | / 或
    非          NOT / ! / ~ / 非
    常量        true/真/yes/1, false/假/no/0
    示例        S1 AND (S2 OR NOT S3)
                非S1 与 (S2 或 S3)

解析结果是由 tuple/str 组成的纯数据 AST：
    ("var", 名称)
    ("not", 子式)
    ("and", 子式, ...)
    ("or",  子式, ...)
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple

# 操作符归一化表
_AND_WORDS = {"AND", "&&", "&", "与", "且"}
_OR_WORDS = {"OR", "||", "|", "或"}
_NOT_WORDS = {"NOT", "!", "~", "非"}
_TRUE_WORDS = {"TRUE", "真", "YES", "1"}
_FALSE_WORDS = {"FALSE", "假", "NO", "0"}


class ConditionError(ValueError):
    """条件语法或变量引用错误。"""


Token = Tuple[str, str, int]  # (类型, 值, 起始位置)


def tokenize(text: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN", "(", i))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN", ")", i))
            i += 1
            continue
        if ch in "!~":
            tokens.append(("NOT", ch, i))
            i += 1
            continue
        if ch == "&":
            # 允许 & 和 &&
            j = i + 1
            if j < n and text[j] == "&":
                j += 1
            tokens.append(("AND", text[i:j], i))
            i = j
            continue
        if ch == "|":
            j = i + 1
            if j < n and text[j] == "|":
                j += 1
            tokens.append(("OR", text[i:j], i))
            i = j
            continue
        # 单词：字母/数字/下划线，以及中文操作符/常量
        # 中文单字操作符（与/且/或/非/真/假）允许紧贴变量书写，如 “非S1”“S1或S2”
        j = i
        buf: List[str] = []
        while j < n and (not text[j].isspace() and text[j] not in "()&|!~"):
            ch = text[j]
            if ch in ("与", "且", "或", "非", "真", "假"):
                if buf:
                    break  # 先吐出已累积的单词，中文记号下一轮单独处理
                buf.append(ch)
                j += 1
                break
            buf.append(ch)
            j += 1
        word = "".join(buf)
        up = word.upper()
        if up in _AND_WORDS or word in _AND_WORDS:
            tokens.append(("AND", word, i))
        elif up in _OR_WORDS or word in _OR_WORDS:
            tokens.append(("OR", word, i))
        elif up in _NOT_WORDS or word in _NOT_WORDS:
            tokens.append(("NOT", word, i))
        elif up in _TRUE_WORDS or word in _TRUE_WORDS:
            tokens.append(("TRUE", word, i))
        elif up in _FALSE_WORDS or word in _FALSE_WORDS:
            tokens.append(("FALSE", word, i))
        else:
            # 变量名：字母/下划线开头，后接字母数字下划线
            if not (word[0].isalpha() or word[0] == "_"):
                raise ConditionError(
                    f"无法识别的记号 “{word}”（位置 {i + 1}）"
                )
            for c in word:
                if not (c.isalnum() or c == "_"):
                    raise ConditionError(
                        f"变量名 “{word}” 只能含字母、数字、下划线（位置 {i + 1}）"
                    )
            tokens.append(("VAR", word, i))
        i = j
    return tokens


class _Parser:
    def __init__(self, text: str, allowed: Sequence[str]):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0
        self.allowed = set(allowed)

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> Token | None:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.next()
        if tok is None or tok[0] != kind:
            where = f"位置 {tok[2] + 1}" if tok else "条件末尾"
            raise ConditionError(f"第 {where} 期望 {kind}，实际为 “{tok[1] if tok else '空'}”")
        return tok

    def parse(self) -> tuple:
        if not self.tokens:
            raise ConditionError("条件为空")
        node = self.parse_or()
        if self.peek() is not None:
            tok = self.peek()
            raise ConditionError(f"条件末尾有多余记号 “{tok[1]}”（位置 {tok[2] + 1}）")
        return node

    def parse_or(self) -> tuple:
        parts = [self.parse_and()]
        while self.peek() is not None and self.peek()[0] == "OR":
            self.next()
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else ("or",) + tuple(parts)

    def parse_and(self) -> tuple:
        parts = [self.parse_not()]
        while self.peek() is not None and self.peek()[0] == "AND":
            self.next()
            parts.append(self.parse_not())
        return parts[0] if len(parts) == 1 else ("and",) + tuple(parts)

    def parse_not(self) -> tuple:
        tok = self.peek()
        if tok is not None and tok[0] == "NOT":
            self.next()
            return ("not", self.parse_not())
        return self.parse_atom()

    def parse_atom(self) -> tuple:
        tok = self.next()
        if tok is None:
            raise ConditionError("条件不完整，缺少变量或括号表达式")
        kind, value, start = tok
        if kind == "LPAREN":
            node = self.parse_or()
            self.expect("RPAREN")
            return node
        if kind == "VAR":
            if value not in self.allowed:
                raise ConditionError(
                    f"变量 “{value}” 不在已定义的筛查项中（位置 {start + 1}）"
                )
            return ("var", value)
        if kind == "TRUE":
            return ("true",)
        if kind == "FALSE":
            return ("false",)
        raise ConditionError(f"意外的记号 “{value}”（位置 {start + 1}）")


def parse_condition(text: str, allowed_vars: Sequence[str]) -> tuple:
    """把条件文本解析为 AST，并校验变量都在允许列表内。"""
    if not isinstance(text, str):
        raise ConditionError("条件必须是文本")
    return _Parser(text.strip(), allowed_vars).parse()


def variables_in(node: Any) -> set:
    """收集 AST 中出现的全部变量名。"""
    if not isinstance(node, tuple):
        return set()
    tag = node[0]
    if tag == "var":
        return {node[1]}
    if tag == "not":
        return variables_in(node[1])
    if tag in ("and", "or"):
        out: set = set()
        for child in node[1:]:
            out |= variables_in(child)
        return out
    return set()


def evaluate(node: Any, assignment: dict) -> bool:
    """在给定变量真值（变量名 -> bool）下求值；未出现的变量不影响结果。"""
    tag = node[0]
    if tag == "var":
        return bool(assignment[node[1]])
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag == "not":
        return not evaluate(node[1], assignment)
    if tag == "and":
        return all(evaluate(c, assignment) for c in node[1:])
    if tag == "or":
        return any(evaluate(c, assignment) for c in node[1:])
    raise ConditionError(f"未知 AST 节点 {tag!r}")


def evaluate_mask(node: Any, mask: int, index_of: dict) -> bool:
    """位掩码形式求值：第 i 位为 1 表示第 i 个变量取真。"""
    tag = node[0]
    if tag == "var":
        return bool(mask & (1 << index_of[node[1]]))
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag == "not":
        return not evaluate_mask(node[1], mask, index_of)
    if tag == "and":
        return all(evaluate_mask(c, mask, index_of) for c in node[1:])
    if tag == "or":
        return any(evaluate_mask(c, mask, index_of) for c in node[1:])
    raise ConditionError(f"未知 AST 节点 {tag!r}")


def ast_to_text(node: Any) -> str:
    """把 AST 转回可读文本（调试/回显用）。"""
    tag = node[0]
    if tag == "var":
        return node[1]
    if tag == "true":
        return "true"
    if tag == "false":
        return "false"
    if tag == "not":
        return "NOT " + _wrap(node[1], ("or", "and"))
    if tag == "and":
        return "(" + " AND ".join(_wrap(c, ("or",)) for c in node[1:]) + ")"
    if tag == "or":
        return "(" + " OR ".join(_wrap(c, ("and", "or")) for c in node[1:]) + ")"
    raise ConditionError(f"未知 AST 节点 {tag!r}")


def _wrap(node: Any, parent_tags: tuple) -> str:
    text = ast_to_text(node)
    return text if node[0] not in parent_tags else text
