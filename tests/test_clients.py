"""Tests for the harvesting adapters, using the FakeHttp client (no network).

Each client is checked for: the happy path (right fact from a canned response),
and the honest-failure contract (404 -> not_found, 429 -> rate_limited,
network error -> error, bad json -> error) that keeps one flaky source from
sinking the whole harvest.
"""

from adapters.openalex_client import OpenAlexClient, _normalise_doi
from adapters.openssf_client import OpenSSFClient, parse_github
from adapters.osv_client import OSVClient
from adapters.registry_clients import CondaClient, CranClient, NpmClient

from tests.conftest import FakeHttp, FakePostHttp, FakeResponse


def _by_name(facts):
    return {f.field_name: f for f in facts}


# ── OpenAlex ──────────────────────────────────────────────────────────────────


def test_openalex_ok():
    body = {
        "id": "https://openalex.org/W1",
        "cited_by_count": 229,
        "publication_year": 2013,
        "type": "article",
        "primary_location": {"source": {"display_name": "Frontiers in Neuroinformatics"}},
    }
    http = FakeHttp([("api.openalex.org", FakeResponse(200, body))])
    facts = OpenAlexClient(http).fetch_for_doi("10.3389/fninf.2013.00027")
    f = _by_name(facts)
    assert f["citations"].value == "229"
    assert f["publication_year"].value == "2013"
    assert f["openalex_venue"].value == "Frontiers in Neuroinformatics"
    assert f["citations"].fetch_status == "ok"


def test_openalex_404_is_not_found():
    http = FakeHttp([("api.openalex.org", FakeResponse(404))])
    facts = OpenAlexClient(http).fetch_for_doi("10.0/missing")
    assert facts[0].fetch_status == "not_found"
    assert facts[0].value is None


def test_openalex_429_is_rate_limited():
    http = FakeHttp([("api.openalex.org", FakeResponse(429))])
    facts = OpenAlexClient(http).fetch_for_doi("10.0/x")
    assert facts[0].fetch_status == "rate_limited"


def test_openalex_network_error():
    http = FakeHttp(raise_on="api.openalex.org")
    facts = OpenAlexClient(http).fetch_for_doi("10.0/x")
    assert facts[0].fetch_status == "error"


def test_openalex_rejects_non_doi_without_calling():
    http = FakeHttp()
    facts = OpenAlexClient(http).fetch_for_doi("not-a-doi")
    assert facts[0].fetch_status == "not_found"
    assert http.calls == []  # never hit the network for a non-DOI


def test_normalise_doi_strips_url_prefix():
    assert _normalise_doi("https://doi.org/10.5281/zenodo.1") == "10.5281/zenodo.1"
    assert _normalise_doi("10.5281/zenodo.1") == "10.5281/zenodo.1"


# ── Registry clients ──────────────────────────────────────────────────────────


def test_conda_sums_file_downloads():
    body = {"files": [{"ndownloads": 1000}, {"ndownloads": 610578}]}
    http = FakeHttp([("api.anaconda.org", FakeResponse(200, body))])
    facts = CondaClient(http).fetch_for_package("nibabel")
    assert facts[0].field_name == "conda_downloads"
    assert facts[0].value == "611578"
    assert facts[0].fetch_status == "ok"


def test_conda_404_not_found():
    http = FakeHttp([("api.anaconda.org", FakeResponse(404))])
    facts = CondaClient(http).fetch_for_package("nope")
    assert facts[0].fetch_status == "not_found"


def test_npm_ok():
    http = FakeHttp([("api.npmjs.org", FakeResponse(200, {"downloads": 12345}))])
    facts = NpmClient(http).fetch_for_package("react")
    assert facts[0].value == "12345"
    assert facts[0].fetch_status == "ok"


def test_npm_error_body_is_not_found():
    # npm returns 200 with {"error": "... not found"} for a missing package.
    http = FakeHttp([("api.npmjs.org", FakeResponse(200, {"error": "package x not found"}))])
    facts = NpmClient(http).fetch_for_package("x")
    assert facts[0].fetch_status == "not_found"


def test_cran_ok():
    http = FakeHttp([("cranlogs", FakeResponse(200, [{"downloads": 3503, "package": "admiral"}]))])
    facts = CranClient(http).fetch_for_package("admiral")
    assert facts[0].value == "3503"
    assert facts[0].fetch_status == "ok"


def test_cran_zero_downloads_is_not_found():
    # 0 can mean "not on CRAN" — treated as not_found to avoid a misleading zero.
    http = FakeHttp([("cranlogs", FakeResponse(200, [{"downloads": 0, "package": "x"}]))])
    facts = CranClient(http).fetch_for_package("x")
    assert facts[0].fetch_status == "not_found"


# ── OpenSSF Scorecard ─────────────────────────────────────────────────────────


def test_openssf_ok():
    body = {"score": 7.8, "date": "2026-11-16",
            "checks": [{"name": "Maintained", "score": 10}, {"name": "Code-Review", "score": 9}]}
    http = FakeHttp([("securityscorecards.dev", FakeResponse(200, body))])
    facts = OpenSSFClient(http).fetch_for_repo("https://github.com/Project-MONAI/MONAI")
    f = _by_name(facts)
    assert f["ssf_score"].value == "7.8"
    assert f["ssf_maintained"].value == "10"
    assert f["ssf_code_review"].value == "9"


def test_openssf_404_not_in_db():
    http = FakeHttp([("securityscorecards.dev", FakeResponse(404))])
    facts = OpenSSFClient(http).fetch_for_repo("https://github.com/a/b")
    assert facts[0].fetch_status == "not_found"


def test_openssf_non_github_skipped_without_call():
    http = FakeHttp()
    facts = OpenSSFClient(http).fetch_for_repo("https://gitlab.com/a/b")
    assert facts[0].fetch_status == "not_found"
    assert http.calls == []  # never calls Scorecard for a non-GitHub repo


def test_parse_github():
    assert parse_github("https://github.com/owner/repo") == ("owner", "repo")
    assert parse_github("https://gitlab.com/owner/repo") is None


# ── OSV ───────────────────────────────────────────────────────────────────────


def test_osv_counts_vulns():
    body = {"vulns": [{"id": "GHSA-1"}, {"id": "GHSA-2"}, {"id": "GHSA-3"}]}
    http = FakePostHttp(FakeResponse(200, body))
    facts = OSVClient(http).fetch_for_package("pillow", "PyPI")
    assert facts[0].field_name == "osv_known_vulns"
    assert facts[0].value == "3"
    assert facts[0].fetch_status == "ok"


def test_osv_no_vulns_is_zero():
    http = FakePostHttp(FakeResponse(200, {}))
    facts = OSVClient(http).fetch_for_package("clean-pkg", "PyPI")
    assert facts[0].value == "0"
    assert facts[0].fetch_status == "ok"


def test_osv_429_rate_limited():
    http = FakePostHttp(FakeResponse(429))
    facts = OSVClient(http).fetch_for_package("x", "PyPI")
    assert facts[0].fetch_status == "rate_limited"
