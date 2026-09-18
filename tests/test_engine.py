"""判定引擎单元测试：分桶、规则、解释链、生效窗口。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.engine import (
    ActivationWindow,
    Rule,
    RuleEvaluationError,
    Variant,
    decide,
    evaluate_rule,
    inactive_decision,
    pick_variant_by_weight,
    stable_bucket,
    window_status,
)

VARIANTS = [Variant("on", 10), Variant("off", 90)]
RULES = [
    Rule(2, "region", "in", ["cn", "sg"], "on"),
    Rule(1, "employee", "eq", [True], "on"),
]


def test_stable_bucket_is_deterministic_and_in_range():
    values = {stable_bucket("flag", f"user-{i}") for i in range(500)}
    assert values <= set(range(100))
    for _ in range(10):
        assert stable_bucket("flag", "user-42") == stable_bucket("flag", "user-42")


def test_different_keys_differ():
    # 不同 flag_key 通常应产生不同桶（哈希空间足够）
    buckets = {stable_bucket(f"flag-{i}", "user") for i in range(20)}
    assert len(buckets) > 1


def test_pick_variant_respects_ranges():
    assert pick_variant_by_weight(VARIANTS, 0)[0] == "on"
    assert pick_variant_by_weight(VARIANTS, 9)[0] == "on"
    assert pick_variant_by_weight(VARIANTS, 10)[0] == "off"
    assert pick_variant_by_weight(VARIANTS, 99)[0] == "off"


def test_missing_attribute_is_miss():
    hit, reason = evaluate_rule(RULES[1], {})
    assert hit is False
    assert "缺少属性" in reason
    hit, _ = evaluate_rule(RULES[1], {"employee": None})
    assert hit is False


def test_priority_order_first_hit_wins():
    # employee 规则 priority=1 先命中，即便 region 也满足
    d = decide("f", "u1", {"employee": True, "region": "cn"}, VARIANTS, RULES)
    assert d.decision_path == "rule"
    assert d.variant_key == "on"
    assert d.matched_rule.attribute == "employee"
    assert d.bucket_value is None
    assert d.explanation[-1]["step"] == "rule_selected"


def test_lower_priority_rule_can_hit_after_first_misses():
    # employee 属性缺失 -> 第一条未命中；region 命中第二条
    d = decide("f", "u2", {"region": "cn"}, VARIANTS, RULES)
    assert d.decision_path == "rule"
    assert d.matched_rule.attribute == "region"
    assert d.matched_rule_index == 1


def test_fallback_to_bucket_and_explanation():
    d = decide("f", "u3", {"region": "us"}, VARIANTS, RULES)
    assert d.decision_path == "bucket"
    assert d.bucket_value == stable_bucket("f", "u3")
    steps = [e["step"] for e in d.explanation]
    assert steps == [
        "rules_loaded",
        "rule_evaluated",
        "rule_evaluated",
        "no_rule_matched",
        "bucket_computed",
        "variant_selected",
    ]


def test_rule_referencing_unknown_variant_raises():
    bad = [Rule(1, "region", "eq", ["cn"], "ghost")]
    with pytest.raises(RuleEvaluationError):
        decide("f", "u", {"region": "cn"}, VARIANTS, bad)


def test_numeric_and_string_operators():
    assert evaluate_rule(Rule(1, "age", "gte", [18], "on"), {"age": 20})[0] is True
    assert evaluate_rule(Rule(1, "age", "gte", [18], "on"), {"age": "x"})[0] is False
    assert evaluate_rule(Rule(1, "email", "suffix", ["@corp.com"], "on"),
                         {"email": "a@corp.com"})[0] is True
    assert evaluate_rule(Rule(1, "email", "suffix", ["@corp.com"], "on"),
                         {"email": 123})[0] is False


def test_zero_weight_variant_never_bucketed():
    vs = [Variant("a", 0), Variant("b", 100)]
    for b in range(100):
        assert pick_variant_by_weight(vs, b)[0] == "b"


# ---------------------------------------------------------------------------
# 版本化生效窗口（半开区间 [active_from, active_until)）
# ---------------------------------------------------------------------------

_FROM = datetime(2026, 9, 20, 8, 0, 0)
_UNTIL = datetime(2026, 9, 22, 8, 0, 0)
_WIN = ActivationWindow(_FROM, _UNTIL, "off")


def test_window_active_between_bounds():
    inside, reason = window_status(_WIN, datetime(2026, 9, 21, 8, 0, 0))
    assert inside is True
    assert "位于生效窗口" in reason


def test_window_active_from_is_inclusive():
    inside, _ = window_status(_WIN, _FROM)
    assert inside is True


def test_window_active_until_is_exclusive():
    inside, reason = window_status(_WIN, _UNTIL)
    assert inside is False
    assert "已失效" in reason


def test_window_before_start_is_inactive():
    inside, reason = window_status(_WIN, datetime(2026, 9, 20, 7, 59, 59))
    assert inside is False
    assert "尚未生效" in reason


def test_window_after_end_is_inactive():
    inside, _ = window_status(_WIN, datetime(2026, 9, 23))
    assert inside is False


def test_window_only_from():
    win = ActivationWindow(_FROM, None, "off")
    assert window_status(win, datetime(2026, 9, 21))[0] is True
    assert window_status(win, datetime(2026, 9, 1))[0] is False


def test_window_only_until():
    win = ActivationWindow(None, _UNTIL, "off")
    assert window_status(win, datetime(2026, 9, 21))[0] is True
    assert window_status(win, _UNTIL)[0] is False


def test_window_status_normalizes_aware_datetime_to_utc():
    # +08:00 的 2026-09-20T16:00:00 == UTC 08:00:00 == active_from（含）
    aware_from = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert window_status(_WIN, aware_from)[0] is True


def test_inactive_decision_shape():
    d = inactive_decision(_WIN, "窗口尚未生效")
    assert d.decision_path == "inactive"
    assert d.variant_key == "off"
    assert d.bucket_value is None
    assert d.matched_rule is None
    assert d.matched_rule_index is None
    steps = [e["step"] for e in d.explanation]
    assert steps == ["activation_window_loaded", "inactive_selected"]
    loaded = d.explanation[0]["detail"]
    assert loaded["active_from"] == "2026-09-20T08:00:00Z"
    assert loaded["active_until"] == "2026-09-22T08:00:00Z"
    assert loaded["inactive_variant_key"] == "off"
