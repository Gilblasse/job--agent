"""Board discovery: recognizing an ATS tenant from a URL or an embedded careers page."""

from __future__ import annotations

import pytest

from jobagent.sources.discovery import boards_in_html, extract_board


class TestIcimsDiscovery:
    """iCIMS tenants live at ``{label}.icims.com``; the label alone is the board token."""

    def test_an_icims_tenant_is_recognised_from_its_search_url(self):
        """``icims.com`` was unknown to ``extract_board``, so no iCIMS URL could be routed."""
        ref = extract_board("https://careers-48forty.icims.com/jobs/search?ss=1")
        assert ref is not None
        assert (ref.platform, ref.token) == ("icims", "careers-48forty")
        assert ref.registry_token() == ref.token

        job = extract_board("https://external-92y.icims.com/jobs/5819/some-slug/job?in_iframe=1")
        assert job is not None
        assert (job.platform, job.token) == ("icims", "external-92y")

    @pytest.mark.parametrize(
        "url",
        ["https://www.icims.com/careers",
         "https://icims.com",
         "https://media.icims.com/x",
         "https://community.icims.com/s/article/x",
         "https://a.b.icims.com/"],
    )
    def test_icims_vendor_hosts_are_not_tenants(self, url):
        """The vendor's own subdomains would otherwise register as employer boards."""
        assert extract_board(url) is None

    def test_an_embedded_icims_iframe_is_discovered(self):
        """Employers embed iCIMS in an iframe on their own careers page; the bare-URL scan
        did not list ``icims.com``, so the board in the markup was invisible."""
        html = (
            '<html><iframe src="https://careers-acme.icims.com/jobs/search?ss=1&in_iframe=1">'
            "</iframe></html>"
        )
        assert [b.key() for b in boards_in_html(html, "https://acme.com/careers")] == [
            ("icims", "careers-acme")
        ]

        bare = "<p>Apply at https://careers-acme.icims.com/jobs/search today</p>"
        assert [b.key() for b in boards_in_html(bare, "https://acme.com/careers")] == [
            ("icims", "careers-acme")
        ]
