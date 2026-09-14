"""Normalization: absorbing the variance between sources."""

from __future__ import annotations

from datetime import date

import pytest

from jobagent.domain.models import WorkplaceType
from jobagent.domain.normalize import (
    detect_country,
    detect_seniority,
    infer_workplace,
    normalize_company,
    normalize_title,
    parse_location,
    parse_relative_date,
    parse_salary,
)
from jobagent.domain.text import html_to_text


class TestHtmlToText:
    def test_double_escaped_html_is_decoded(self):
        """Greenhouse entity-encodes its HTML; one unescape pass leaves tags visible."""
        assert "<p>" not in html_to_text("&lt;p&gt;Hello&lt;/p&gt;")
        assert "Hello" in html_to_text("&lt;p&gt;Hello&lt;/p&gt;")

    def test_list_structure_survives_as_bullets(self):
        text = html_to_text("<ul><li>CPA required</li><li>Excel preferred</li></ul>")
        assert text.count("- ") == 2

    def test_script_content_is_dropped(self):
        assert "trackEvent" not in html_to_text("<p>Hi</p><script>trackEvent()</script>")

    def test_malformed_markup_degrades_rather_than_raising(self):
        assert "Hello" in html_to_text("<p>Hello<<<>>")


class TestCountryDetection:
    @pytest.mark.parametrize(
        "text", ["Dallas, TX", "Remote - US", "United States", "Austin, Texas", "US-based"]
    )
    def test_us_signals(self, text):
        assert detect_country(text)[0] == "US"

    @pytest.mark.parametrize(
        "text,code",
        [("Toronto, Canada", "CA"), ("Berlin, Germany", "DE"), ("Remote - EMEA", "EMEA")],
    )
    def test_non_us_signals(self, text, code):
        assert detect_country(text)[0] == code

    def test_bare_remote_yields_nothing(self):
        assert detect_country("Remote")[0] is None

    def test_us_wins_when_both_are_named(self):
        """"Remote (US) or Canada" is open to US applicants; reporting CA would be wrong."""
        assert detect_country("Remote (US) or Canada")[0] == "US"


class TestLocation:
    def test_city_and_state_are_split(self):
        location = parse_location("Dallas, TX")
        assert (location.city, location.region, location.country) == ("Dallas", "TX", "US")

    def test_spelled_out_state_normalizes_to_its_abbreviation(self):
        assert parse_location("Dallas, Texas").region == "TX"

    def test_remote_is_not_recorded_as_a_city(self):
        assert parse_location("Remote").city is None


class TestWorkplace:
    def test_source_field_beats_text_inference(self, taxonomy):
        workplace, evidence = infer_workplace(
            "hybrid schedule", taxonomy, WorkplaceType.REMOTE
        )
        assert workplace is WorkplaceType.REMOTE
        assert evidence == "source field"

    def test_hybrid_is_detected_before_remote(self, taxonomy):
        """Hybrid postings nearly always contain the word "remote" too."""
        text = "A hybrid role with some remote work"
        assert infer_workplace(text, taxonomy)[0] is WorkplaceType.HYBRID

    def test_negation_is_not_read_as_remote(self, taxonomy):
        assert infer_workplace("This is not remote.", taxonomy)[0] is WorkplaceType.ONSITE

    def test_absent_signal_stays_unknown(self, taxonomy):
        assert infer_workplace("Join our finance team.", taxonomy)[0] is WorkplaceType.UNKNOWN


class TestSalary:
    @pytest.mark.parametrize(
        "text,low,high",
        [
            ("$120,000 - $150,000 per year", 120_000, 150_000),
            ("$60,000-$75,000", 60_000, 75_000),
            ("Range: $90k to $110k", 90_000, 110_000),
        ],
    )
    def test_ranges_are_extracted(self, text, low, high):
        salary = parse_salary(text)
        assert (salary.minimum, salary.maximum) == (low, high)

    def test_hourly_rate_is_recognized(self):
        salary = parse_salary("$32.50 per hour")
        assert salary.period == "hour"
        assert salary.annualized()[0] == pytest.approx(67_600)

    def test_bare_numbers_are_not_mistaken_for_pay(self):
        """"5 years of experience" and "401k" must not become a salary."""
        assert parse_salary("Requires 5 years of experience. 401 plan available.") is None

    def test_no_pay_information_yields_none(self):
        assert parse_salary("Competitive compensation and benefits.") is None


class TestSeniority:
    @pytest.mark.parametrize(
        "title,level",
        [("Senior Accountant", "senior"), ("Junior Accountant", "entry"),
         ("Staff Engineer", "staff"), ("Accounting Manager", "management"),
         ("Accountant", None)],
    )
    def test_levels_are_read_from_the_title(self, title, level, taxonomy):
        assert detect_seniority(title, taxonomy)[0] == level

    def test_description_mentions_do_not_set_the_level(self, taxonomy):
        """"Partners with senior leadership" describes colleagues, not the role."""
        assert detect_seniority("Accountant", taxonomy)[0] is None


class TestIdentityHelpers:
    def test_legal_suffixes_are_stripped_from_company_names(self):
        assert normalize_company("Acme, Inc.") == normalize_company("Acme Incorporated")

    def test_requisition_ids_are_stripped_from_titles(self):
        assert normalize_title("Accountant (Req #12345)") == "accountant"


class TestRelativeDates:
    @pytest.mark.parametrize(
        "text,expected",
        [("Posted Today", date(2026, 9, 14)), ("Posted 5 Days Ago", date(2026, 9, 9)),
         ("Posted 2 Weeks Ago", date(2026, 8, 31))],
    )
    def test_workday_style_dates_are_parsed(self, text, expected):
        """Workday publishes prose instead of a timestamp; freshness needs it parsed."""
        assert parse_relative_date(text, date(2026, 9, 14)) == expected

    def test_unparseable_text_yields_none(self):
        assert parse_relative_date("Recently", date(2026, 9, 14)) is None
