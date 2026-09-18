"""Dismissing a job: the reason is the record, the rule is what changes the search."""

from __future__ import annotations

import pytest

from jobagent.domain.feedback import (
    DismissRule,
    apply_rules,
    describe,
    parse_rule,
    suggest_rules,
    title_conflicts,
)
from jobagent.domain.normalize import title_core
from jobagent.domain.spec import SearchSpec


def spec(**overrides) -> SearchSpec:
    data = {"name": "t", "titles": ["Junior Accountant"], "related_titles": ["Bookkeeper"]}
    return SearchSpec.model_validate({**data, **overrides})


class TestParseRule:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("employer", DismissRule("employer")),
            ("hide", DismissRule("hide")),
            ("seniority", DismissRule("seniority")),
            ("seniority=senior", DismissRule("seniority", "senior")),
            ("title=Account Director", DismissRule("title", "Account Director")),
            ("duty=cold calling", DismissRule("duty", "cold calling")),
            ("skill=Angular", DismissRule("skill", "Angular")),
            ("anywhere=crypto", DismissRule("anywhere", "crypto")),
            ("  Title = Account Director ", DismissRule("title", "Account Director")),
        ],
    )
    def test_every_form(self, text, expected):
        assert parse_rule(text) == expected

    @pytest.mark.parametrize("text", ["banana", "title", "duty=", "employer=Acme", "hide=x"])
    def test_junk_is_refused_with_the_forms_named(self, text):
        with pytest.raises(ValueError):
            parse_rule(text)


class TestApplyRules:
    def test_each_kind_lands_in_its_own_list(self, taxonomy):
        rules = [
            DismissRule("employer", "Acme Corp"),
            DismissRule("title", "Account Director"),
            DismissRule("duty", "cold calling"),
            DismissRule("skill", "Angular"),
            DismissRule("anywhere", "crypto"),
            DismissRule("seniority", "senior"),
            DismissRule("hide"),
        ]
        updated = apply_rules(spec(), rules, taxonomy)
        assert updated.excluded_companies == ["Acme Corp"]
        assert updated.excluded_titles == ["Account Director"]
        assert updated.responsibilities_exclude == ["cold calling"]
        assert updated.excluded_skills == ["Angular"]
        assert updated.deal_breakers == ["crypto"]
        assert updated.seniority_exclude == ["senior"]

    def test_the_original_spec_is_untouched(self, taxonomy):
        original = spec()
        apply_rules(original, [DismissRule("employer", "Acme")], taxonomy)
        assert original.excluded_companies == []

    def test_duplicates_are_dropped_case_insensitively(self, taxonomy):
        updated = apply_rules(
            spec(excluded_companies=["Acme Corp"]),
            [DismissRule("employer", "acme corp")], taxonomy,
        )
        assert updated.excluded_companies == ["Acme Corp"]

    def test_hide_changes_nothing(self, taxonomy):
        assert apply_rules(spec(), [DismissRule("hide")], taxonomy) == spec()

    def test_an_unknown_level_is_refused(self, taxonomy):
        with pytest.raises(ValueError, match="unknown seniority level"):
            apply_rules(spec(), [DismissRule("seniority", "guru")], taxonomy)

    def test_a_valueless_rule_is_refused(self, taxonomy):
        with pytest.raises(ValueError):
            apply_rules(spec(), [DismissRule("title", "")], taxonomy)


class TestTitleConflicts:
    def test_a_phrase_inside_a_wanted_title_is_a_conflict(self):
        assert title_conflicts(spec(), "Accountant") == ["Junior Accountant"]

    def test_inflection_counts(self):
        conflicts = title_conflicts(spec(titles=["Bookkeepers"]), "bookkeeper")
        assert conflicts == ["Bookkeepers", "Bookkeeper"]

    def test_a_phrase_that_merely_contains_a_wanted_title_is_not(self):
        assert title_conflicts(spec(), "Junior Accountant Trainee") == []

    def test_an_unrelated_phrase_is_not(self):
        assert title_conflicts(spec(), "Account Director") == []


class TestSuggestions:
    def test_the_job_suggests_its_employer_core_title_levels_and_hide(self, taxonomy):
        rules = suggest_rules("Senior Accountant - Dallas, TX", "Acme Corp", taxonomy)
        assert rules[0] == DismissRule("employer", "Acme Corp")
        assert rules[1] == DismissRule("title", "Senior Accountant")
        assert DismissRule("seniority", "senior") in rules
        assert rules[-1] == DismissRule("hide")

    def test_a_title_with_no_level_suggests_none(self, taxonomy):
        kinds = [r.kind for r in suggest_rules("Accountant", "Acme", taxonomy)]
        assert "seniority" not in kinds


class TestDescribe:
    def test_each_rule_names_its_scope(self):
        assert describe(DismissRule("title", "x")).startswith("TITLE:")
        assert describe(DismissRule("duty", "x")).startswith("DUTIES:")
        assert describe(DismissRule("skill", "x")).startswith("ANYWHERE (as a name):")
        assert describe(DismissRule("anywhere", "x")).startswith("ANYWHERE:")
        assert describe(DismissRule("employer", "x")).startswith("EMPLOYER:")
        assert describe(DismissRule("seniority", "x")).startswith("LEVEL:")
        assert "no rule" in describe(DismissRule("hide"))


class TestTitleCore:
    @pytest.mark.parametrize(
        ("title", "core"),
        [
            ("Accountant - Dallas, TX", "Accountant"),
            ("Accountant, Austin TX", "Accountant"),
            ("Project Manager | Remote", "Project Manager"),
            ("Sales Manager - London, United Kingdom", "Sales Manager"),
            ("Staff Engineer - Req #12345", "Staff Engineer"),
            ("Accountant - Payroll", "Accountant - Payroll"),
            ("Manager, Accounts Payable and Expenses", "Manager, Accounts Payable and Expenses"),
        ],
    )
    def test_qualifiers_go_and_the_role_stays_in_its_own_casing(self, title, core):
        assert title_core(title) == core

    def test_a_hyphenated_title_is_not_cut_at_its_own_hyphen(self):
        """Regression: the suffix was matched from the left, so this became "Front"."""
        assert title_core("Front-End Developer, Austin TX") == "Front-End Developer"
        assert title_core("Co-op Student - Remote") == "Co-op Student"
