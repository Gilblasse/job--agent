"""Phrase matching: the one place every gate's idea of "mentions" is defined."""

from __future__ import annotations

import pytest

from jobagent.domain.text import find_phrase


class TestWordJoinsAreFlexible:
    """A user who types one spelling of a compound means all of them.

    "Front-end developer" typed with a hyphen used to match only the hyphenated form,
    while "Front End Developer" matched the spaced and hyphenated forms but not the
    closed one. Postings use all three interchangeably, so a title list had to spell
    every variant out or silently miss two thirds of the market.
    """

    @pytest.mark.parametrize("typed", ["front-end developer", "front end developer",
                                       "Front-End Developer"])
    @pytest.mark.parametrize("posted", ["Front-End Developer", "Front End Developer",
                                        "Frontend Developer", "Front/End Developer"])
    def test_any_spelling_finds_any_spelling(self, typed, posted):
        assert find_phrase(f"Hiring a {posted} now", typed) is not None

    def test_closed_spelling_typed_matches_only_itself(self):
        """Nothing tells us where "frontend" divides, so it stays one word."""
        assert find_phrase("Frontend Developer", "frontend developer") is not None
        assert find_phrase("Front End Developer", "frontend developer") is None

    def test_a_join_never_crosses_a_word_boundary(self):
        assert find_phrase("confrontend", "front end") is None
        assert find_phrase("front ending", "front end") is None

    def test_inflection_applies_to_the_last_word_after_a_hyphen(self):
        assert find_phrase("our e-commerce operations", "e-commerce", inflect=True) is not None
        assert find_phrase("ecommerce sites", "e-commerce", inflect=True) is not None

    def test_casing_rules_are_unchanged(self):
        assert find_phrase("react to feedback", "React", match_case=True) is None
        assert find_phrase("React components", "React", match_case=True) is not None


class TestShortWordsMatchAsWords:
    """Why the wizard asks about entries like these: they are legal, just wide."""

    def test_a_two_letter_word_matches_the_ordinary_verb(self):
        assert find_phrase("we go above and beyond", "go") is not None

    def test_a_single_letter_matches_wherever_it_stands_alone(self):
        assert find_phrase("Plan C for the launch", "c") is not None
        assert find_phrase("cloud services", "c") is None
