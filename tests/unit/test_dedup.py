"""Deduplication and authority ranking."""

from __future__ import annotations

import pytest

from jobagent.domain.dedup import (
    canonical_url,
    choose_canonical,
    cluster_postings,
    identity_for,
    url_authority,
)
from jobagent.domain.models import AuthorityTier
from tests.conftest import make_posting


class TestCanonicalUrl:
    def test_tracking_parameters_are_removed(self):
        assert canonical_url(
            "https://boards.greenhouse.io/acme/jobs/1?gh_src=x&utm_source=y"
        ) == "https://boards.greenhouse.io/acme/jobs/1"

    def test_host_case_www_and_fragment_are_normalized(self):
        assert canonical_url("https://WWW.Acme.com/jobs/1/#apply") == "https://acme.com/jobs/1"

    def test_meaningful_query_parameters_are_kept(self):
        assert "id=55" in canonical_url("https://acme.com/careers?id=55&utm_medium=email")


class TestIdentity:
    def test_the_same_role_from_two_sources_gets_one_identity(self):
        assert identity_for("Acme, Inc.", "Senior Accountant (Remote)", "Dallas, TX") == \
               identity_for("Acme Incorporated", "senior accountant", "Dallas, Texas")

    def test_different_roles_stay_distinct(self):
        assert identity_for("Acme", "Accountant", "Dallas, TX") != \
               identity_for("Acme", "Controller", "Dallas, TX")

    def test_same_role_in_different_cities_stays_distinct(self):
        assert identity_for("Acme", "Accountant", "Dallas, TX") != \
               identity_for("Acme", "Accountant", "Austin, TX")

    def test_metro_spellings_collapse(self):
        assert identity_for("Acme", "Accountant", "Dallas-Fort Worth, TX") == \
               identity_for("Acme", "Accountant", "Dallas, TX")


class TestAuthority:
    def test_employer_domain_outranks_its_ats(self):
        assert url_authority("https://careers.acme.com/jobs/1", "acme.com") > \
               url_authority("https://boards.greenhouse.io/acme/jobs/1")

    def test_ats_outranks_an_unknown_host(self):
        assert url_authority("https://jobs.lever.co/acme/1") > \
               url_authority("https://some-aggregator.example/jobs/1")

    def test_icims_urls_rank_as_official_ats(self):
        """``icims.com`` was missing from ATS_HOSTS, so an iCIMS record lost every tie."""
        assert (
            url_authority("https://careers-acme.icims.com/jobs/5819/slug/job", "acme.com")
            is AuthorityTier.OFFICIAL_ATS
        )
        assert (
            url_authority("https://some-aggregator.example/jobs/1", "acme.com")
            is AuthorityTier.UNVERIFIED
        )


class TestClustering:
    def test_postings_sharing_a_url_merge_even_with_different_titles(self):
        """A shared canonical URL is conclusive; wording differences are not."""
        url = "https://boards.greenhouse.io/acme/jobs/9"
        a = make_posting(title="Accountant", url=url, external_id="9")
        b = make_posting(
            title="Accountant - General Ledger", url=url + "?utm_source=x",
            external_id="9", source="careersite",
        )
        assert len(cluster_postings([a, b])) == 1

    def test_unrelated_postings_do_not_merge(self):
        a = make_posting(title="Accountant", external_id="1")
        b = make_posting(title="Warehouse Supervisor", external_id="2",
                         url="https://boards.greenhouse.io/acme/jobs/2")
        assert len(cluster_postings([a, b])) == 2

    def test_canonical_record_is_the_most_authoritative(self):
        ats = make_posting(
            title="Accountant", description="Full description here",
            authority=AuthorityTier.OFFICIAL_ATS,
        )
        site = make_posting(
            title="Accountant", description="Full description here",
            url="https://careers.acme.com/jobs/1", source="careersite",
            authority=AuthorityTier.EMPLOYER_SITE,
        )
        assert choose_canonical([ats, site]) is site

    def test_a_record_with_a_description_wins_among_equals(self):
        """Gates read the description; a stub record would make them all unverifiable."""
        empty = make_posting(description="", external_id="1")
        full = make_posting(description="Own the monthly close.", external_id="2")
        assert choose_canonical([empty, full]) is full


class TestClusteringIsOrderIndependent:
    """Merging is transitive, so arrival order must not change the grouping.

    Regression: an earlier version reassigned each posting to the identity it collided
    with, which split a group whenever the colliding record happened to be seen first.
    """

    def test_transitive_merge_via_a_shared_url(self):
        shared = "https://boards.greenhouse.io/acme/jobs/7"
        a = make_posting(title="Accountant", url=shared, external_id="7")
        b = make_posting(title="Accountant", url="https://careers.acme.com/jobs/7",
                         external_id="7", source="careersite")
        c = make_posting(title="Accountant (General Ledger)", url=shared + "?utm_source=x",
                         external_id="9", source="aggregate")
        assert len(cluster_postings([a, b, c])) == 1

    def test_the_grouping_does_not_depend_on_order(self):
        shared = "https://boards.greenhouse.io/acme/jobs/7"
        a = make_posting(title="Accountant", url=shared, external_id="7")
        b = make_posting(title="Different Role", url=shared, external_id="7",
                         source="careersite")
        c = make_posting(title="Accountant", url="https://acme.com/x", external_id="8",
                         source="careersite")

        sizes = []
        for order in ([a, b, c], [c, b, a], [b, a, c], [c, a, b]):
            clusters = cluster_postings(order)
            sizes.append(sorted(len(group) for group in clusters.values()))
        assert all(size == sizes[0] for size in sizes), sizes

    def test_cluster_keys_are_stable_across_runs(self):
        """The key is persisted, so it must not shift between runs."""
        postings = [
            make_posting(title="Accountant", external_id="1"),
            make_posting(title="Accountant", url="https://careers.acme.com/jobs/1",
                         external_id="1", source="careersite"),
        ]
        first = set(cluster_postings(postings))
        second = set(cluster_postings(list(reversed(postings))))
        assert first == second


class TestAuthorityCannotBeSpoofedBySubstring:
    """Authority decides which link the user is sent to, so lookalikes must not pass."""

    def test_a_lookalike_ats_host_is_not_official(self):
        assert url_authority("https://greenhouse.io.evil.example/acme/jobs/1") \
            is AuthorityTier.UNVERIFIED

    def test_a_lookalike_employer_host_is_not_the_employer(self):
        assert url_authority("https://evilacme.com/jobs/1", "acme.com") \
            is AuthorityTier.UNVERIFIED

    def test_a_real_subdomain_still_counts(self):
        assert url_authority("https://careers.acme.com/jobs/1", "acme.com") \
            is AuthorityTier.EMPLOYER_SITE
        assert url_authority("https://boards.greenhouse.io/acme") is AuthorityTier.OFFICIAL_ATS

    def test_the_bare_domain_still_counts(self):
        assert url_authority("https://acme.com/jobs/1", "acme.com") \
            is AuthorityTier.EMPLOYER_SITE


class TestDiscoveryRejectsLookalikeHosts:
    """A lookalike host must not register as an official ATS board.

    `company add`, careers-page discovery and seed building all route through this, and
    the result decides which link a user is sent to.
    """

    @pytest.mark.parametrize(
        "url",
        ["https://evilgreenhouse.io/acme",
         "https://greenhouse.io.evil.example/acme",
         "https://evilbamboohr.com/careers",
         "https://notlever.co/acme",
         "https://myworkdayjobs.com.evil.example/x"],
    )
    def test_lookalikes_are_rejected(self, url):
        from jobagent.sources.discovery import extract_board

        assert extract_board(url) is None

    @pytest.mark.parametrize(
        "url,platform",
        [("https://boards.greenhouse.io/acme", "greenhouse"),
         ("https://jobs.lever.co/acme", "lever"),
         ("https://acme.bamboohr.com/careers", "bamboohr"),
         ("https://acme.recruitee.com", "recruitee")],
    )
    def test_genuine_boards_still_resolve(self, url, platform):
        from jobagent.sources.discovery import extract_board

        assert extract_board(url).platform == platform
