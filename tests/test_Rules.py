"""Behavior rules: matching, validation, percent resolution."""

import pytest

from orderecho_Rules import RuleError, load_rules, parse_rule

CATCH_ALL = {"name": "default", "match": {"any": True}, "behavior": "full_fill"}


def rules(*entries):
    return load_rules(list(entries) + [dict(CATCH_ALL)], 500)


# --- matching --------------------------------------------------------------

def test_exact_symbol_beats_a_range_that_also_matches():
    ruleset = rules(
        {"name": "exact", "match": {"symbol": "ABC"}, "behavior": "ack_only"},
        {"name": "range", "match": {"first_letter": "A-D"},
         "behavior": "full_fill"},
    )
    assert ruleset.match("ABC").name == "exact"
    assert ruleset.match("ADD").name == "range"


def test_first_match_wins_even_when_a_later_rule_is_narrower():
    ruleset = rules(
        {"name": "range", "match": {"first_letter": "A-D"}, "behavior": "full_fill"},
        {"name": "exact", "match": {"symbol": "ABC"}, "behavior": "ack_only"},
    )
    assert ruleset.match("ABC").name == "range"


def test_lowercase_symbols_match_a_range_after_uppercasing():
    ruleset = rules(
        {"name": "range", "match": {"first_letter": "E-G"}, "behavior": "ack_only"},
    )
    assert ruleset.match("efg").name == "range"
    assert ruleset.match("EFG").name == "range"


def test_exact_symbol_match_is_case_sensitive():
    ruleset = rules(
        {"name": "exact", "match": {"symbol": "ZVZZT"}, "behavior": "ack_only"},
    )
    assert ruleset.match("ZVZZT").name == "exact"
    assert ruleset.match("zvzzt").name == "default"


def test_single_letter_range():
    ruleset = rules(
        {"name": "just-q", "match": {"first_letter": "Q"}, "behavior": "ack_only"},
    )
    assert ruleset.match("QQQ").name == "just-q"
    assert ruleset.match("RRR").name == "default"


def test_range_boundaries_are_inclusive():
    ruleset = rules(
        {"name": "a-to-d", "match": {"first_letter": "A-D"}, "behavior": "ack_only"},
    )
    assert ruleset.match("AAA").name == "a-to-d"
    assert ruleset.match("DDD").name == "a-to-d"
    assert ruleset.match("EEE").name == "default"


def test_catch_all_matches_anything():
    ruleset = rules()
    assert ruleset.match("ANYTHING").name == "default"
    assert ruleset.match("123").name == "default"


# --- config-load failures (§7.2) -------------------------------------------

def test_zero_match_keys_fails_naming_the_rule():
    with pytest.raises(RuleError, match="empty-match"):
        rules({"name": "empty-match", "match": {}, "behavior": "full_fill"})


def test_multiple_match_keys_fails_naming_the_rule():
    with pytest.raises(RuleError, match="two-keys"):
        rules({"name": "two-keys",
               "match": {"symbol": "AAA", "first_letter": "A-D"},
               "behavior": "full_fill"})


def test_unknown_behavior_fails_naming_the_rule():
    with pytest.raises(RuleError, match="odd-behavior"):
        rules({"name": "odd-behavior", "match": {"any": True},
               "behavior": "explode"})


@pytest.mark.parametrize("bad_fill", [0, -5, "0%", "101%", "abc%", "50", 1.5, True])
def test_bad_fill_entry_fails_naming_the_rule(bad_fill):
    with pytest.raises(RuleError, match="bad-fills"):
        rules({"name": "bad-fills", "match": {"any": True},
               "behavior": "partial_fill", "fills": [bad_fill]})


def test_percentages_over_one_hundred_fail_naming_the_rule():
    with pytest.raises(RuleError, match="too-much"):
        rules({"name": "too-much", "match": {"any": True},
               "behavior": "partial_fill", "fills": ["60%", "50%"]})


def test_percentages_summing_to_exactly_one_hundred_are_allowed():
    ruleset = rules({"name": "all-of-it", "match": {"first_letter": "A"},
                     "behavior": "partial_fill", "fills": ["60%", "40%"]})
    assert ruleset.match("AAA").resolve_fills(1000) == [600, 400]


def test_unknown_then_value_fails_naming_the_rule():
    with pytest.raises(RuleError, match="odd-then"):
        rules({"name": "odd-then", "match": {"any": True},
               "behavior": "partial_fill", "fills": ["50%"], "then": "explode"})


def test_partial_fill_without_fills_fails_naming_the_rule():
    with pytest.raises(RuleError, match="no-fills"):
        rules({"name": "no-fills", "match": {"any": True},
               "behavior": "partial_fill"})


def test_rule_set_without_a_catch_all_fails():
    with pytest.raises(RuleError, match="only-abc"):
        load_rules([{"name": "only-abc", "match": {"symbol": "ABC"},
                     "behavior": "full_fill"}], 500)


def test_catch_all_that_is_not_last_fails():
    with pytest.raises(RuleError, match="early-catch-all"):
        load_rules(
            [{"name": "early-catch-all", "match": {"any": True},
              "behavior": "full_fill"},
             {"name": "unreachable", "match": {"symbol": "ABC"},
              "behavior": "ack_only"},
             dict(CATCH_ALL)],
            500,
        )


def test_duplicate_rule_names_fail():
    with pytest.raises(RuleError, match="twice"):
        rules({"name": "twice", "match": {"symbol": "A"}, "behavior": "ack_only"},
              {"name": "twice", "match": {"symbol": "B"}, "behavior": "ack_only"})


def test_bad_first_letter_format_fails_naming_the_rule():
    with pytest.raises(RuleError, match="bad-range"):
        rules({"name": "bad-range", "match": {"first_letter": "A-"},
               "behavior": "ack_only"})


def test_backwards_range_fails_naming_the_rule():
    with pytest.raises(RuleError, match="backwards"):
        rules({"name": "backwards", "match": {"first_letter": "Z-A"},
               "behavior": "ack_only"})


def test_unknown_match_key_fails_naming_the_rule():
    with pytest.raises(RuleError, match="odd-key"):
        rules({"name": "odd-key", "match": {"ticker": "AAA"},
               "behavior": "ack_only"})


def test_rule_without_a_name_fails_with_its_index():
    with pytest.raises(RuleError, match=r"rules\[0\]"):
        load_rules([{"match": {"any": True}, "behavior": "full_fill"}], 500)


def test_empty_rule_list_fails():
    with pytest.raises(RuleError):
        load_rules([], 500)


# --- percent resolution ----------------------------------------------------

def test_percent_fills_floor():
    rule = parse_rule({"name": "p", "match": {"any": True},
                       "behavior": "partial_fill", "fills": ["33%"]})
    assert rule.resolve_fills(100) == [33]
    assert rule.resolve_fills(10) == [3]        # 3.3 floors to 3
    assert rule.resolve_fills(1000) == [330]


def test_percent_fills_have_a_floor_of_one_share():
    rule = parse_rule({"name": "p", "match": {"any": True},
                       "behavior": "partial_fill", "fills": ["1%"]})
    assert rule.resolve_fills(10) == [1]        # 0.1 would floor to 0
    assert rule.resolve_fills(1) == [1]


def test_share_fills_are_used_as_given():
    rule = parse_rule({"name": "s", "match": {"any": True},
                       "behavior": "partial_fill", "fills": [1, 2, 3, 405]})
    assert rule.resolve_fills(1000) == [1, 2, 3, 405]
    assert rule.resolve_fills(2) == [1, 2, 3, 405]   # clamping happens at fill time


def test_mixed_share_and_percent_fills():
    rule = parse_rule({"name": "m", "match": {"any": True},
                       "behavior": "partial_fill", "fills": [100, "25%"]})
    assert rule.resolve_fills(1000) == [100, 250]


# --- delays ----------------------------------------------------------------

def test_rule_delay_overrides_the_default():
    ruleset = rules({"name": "slow", "match": {"first_letter": "A"},
                     "behavior": "full_fill", "delay_ms": 2000})
    assert ruleset.match("AAA").delay_for(ruleset.default_delay_ms) == 2000
    assert ruleset.match("ZZZ").delay_for(ruleset.default_delay_ms) == 500


def test_reject_rule_gets_default_code_and_text():
    rule = parse_rule({"name": "r", "match": {"any": True}, "behavior": "reject"})
    assert rule.reject_code == "0"
    assert "r" in rule.text
