"""SPEC-2 + shared/plan-type-whitelist 专项测试(Round 7 P2.3 从 test_spec2_lifecycle.py 抽出)。

覆盖共因 A:
  - SUPPORTED_PLAN_TYPES 常量是 4 元素 frozenset
  - normalize_plan_type 大小写 / 空白 / None / 未知值的归一化语义
  - is_supported_plan 仅对白名单返回 True
"""

import pytest


class TestSupportedPlanTypesConstant:
    def test_supported_plan_types_is_frozenset_with_4_entries(self):
        from autoteam.accounts import SUPPORTED_PLAN_TYPES
        assert isinstance(SUPPORTED_PLAN_TYPES, frozenset)
        assert SUPPORTED_PLAN_TYPES == frozenset({"team", "free", "plus", "pro"})


class TestNormalizePlanType:
    @pytest.mark.parametrize("raw,expected", [
        ("team", "team"),
        ("Team", "team"),
        ("  Free ", "free"),
        ("PLUS", "plus"),
        (None, "unknown"),
        ("", "unknown"),
        ("self_serve_business_usage_based", "self_serve_business_usage_based"),
    ])
    def test_normalize_plan_type(self, raw, expected):
        from autoteam.accounts import normalize_plan_type
        assert normalize_plan_type(raw) == expected


class TestIsSupportedPlan:
    @pytest.mark.parametrize("raw,supported", [
        ("team", True),
        ("Team", True),
        ("free", True),
        ("plus", True),
        ("pro", True),
        ("self_serve_business_usage_based", False),
        ("enterprise", False),
        ("unknown", False),
        ("", False),
        (None, False),
    ])
    def test_is_supported_plan_whitelist_only(self, raw, supported):
        from autoteam.accounts import is_supported_plan
        assert is_supported_plan(raw) is supported
