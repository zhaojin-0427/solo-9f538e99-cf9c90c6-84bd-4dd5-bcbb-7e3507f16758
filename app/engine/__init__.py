# -*- coding: utf-8 -*-
"""判定引擎（零第三方依赖，仅标准库）。"""
from .conditions import (
    ConditionError,
    ast_to_text,
    evaluate,
    evaluate_mask,
    parse_condition,
    variables_in,
)
from .config import (
    MAX_ITEMS,
    AnalysisConfig,
    ConfigError,
    FollowUp,
    build_config,
    config_to_jsonable,
    normalize_answers,
    replace_followups,
    validate_archive,
)
from .solver import AnalysisResult, analyze, build_constraints, decision_depth

__all__ = [
    "ConditionError",
    "ast_to_text",
    "evaluate",
    "evaluate_mask",
    "parse_condition",
    "variables_in",
    "MAX_ITEMS",
    "AnalysisConfig",
    "ConfigError",
    "FollowUp",
    "build_config",
    "config_to_jsonable",
    "normalize_answers",
    "replace_followups",
    "validate_archive",
    "AnalysisResult",
    "analyze",
    "build_constraints",
    "decision_depth",
]
