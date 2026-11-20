"""Tests for core.discovery — the pure identifier-extraction logic.

These lock in every edge case the harvester hit in the field: badge-image
suffixes leaking into package names (.svg), journal URL fragments breaking DOIs
(/full), and Hugging Face org/collection pages being wrongly treated as model
ids. They run with no network (the logic is pure), so they are fast and
deterministic.
"""

from core.discovery import (
    parse_citation_cff,
    parse_conda_meta,
    parse_cran_description,
    parse_package_json,
    parse_pyproject,
    parse_setup_py,
    scan_text,
)


# ── README / free-text scanning ───────────────────────────────────────────────


def test_scan_text_finds_hf_model_and_dataset():
    text = (
        "Model https://huggingface.co/MahmoodLab/CONCH and "
        "dataset https://huggingface.co/datasets/openai/healthbench"
    )
    d = scan_text(text)
    assert d.hf == ["MahmoodLab/CONCH", "openai/healthbench"]


def test_scan_text_rejects_hf_org_and_collection_pages():
    # An org page and a /collections/ page are NOT single artifacts — must be dropped.
    text = (
        "Org https://huggingface.co/wanglab "
        "Collection https://huggingface.co/collections/x/bioreason-abc"
    )
    d = scan_text(text)
    assert d.hf == []


def test_scan_text_extracts_doi_from_url_and_bare():
    text = "Paper: https://doi.org/10.1016/j.ijmedinf.2026.106732 (also 10.5281/zenodo.123)"
    d = scan_text(text)
    assert "10.1016/j.ijmedinf.2026.106732" in d.doi
    assert "10.5281/zenodo.123" in d.doi


def test_scan_text_strips_badge_svg_suffix_from_package_name():
    # Regression: shields.io badge .../pydicom.svg must become "pydicom", not "pydicom.svg".
    text = "[![PyPI](https://img.shields.io/pypi/v/pydicom.svg)](https://pypi.org/project/pydicom)"
    d = scan_text(text)
    assert d.pypi == ["pydicom"]


def test_scan_text_strips_journal_fragment_from_doi():
    # Regression: a Frontiers DOI copied with a trailing /full broke OpenAlex lookups.
    text = "doi: https://doi.org/10.3389/fninf.2013.00027/full"
    d = scan_text(text)
    assert "10.3389/fninf.2013.00027" in d.doi
    assert "10.3389/fninf.2013.00027/full" not in d.doi


def test_scan_text_scoped_npm_name():
    text = "[![npm](https://img.shields.io/npm/v/@acme/widget)](https://npmjs.com)"
    d = scan_text(text)
    assert d.npm == ["@acme/widget"]


def test_scan_text_empty_is_empty_not_error():
    d = scan_text("")
    assert d.hf == [] and d.doi == [] and d.pypi == []


# ── Manifest parsers (declared name is authoritative) ─────────────────────────


def test_parse_pyproject_project_table():
    assert parse_pyproject('[project]\nname = "my-package"\nversion = "1.0"') == "my-package"


def test_parse_pyproject_poetry_table():
    assert parse_pyproject('[tool.poetry]\nname = "poetrypkg"') == "poetrypkg"


def test_parse_setup_py():
    assert parse_setup_py('setup(name="legacy_pkg", version="2")') == "legacy_pkg"


def test_parse_package_json_scoped():
    assert parse_package_json('{"name": "@scope/widget", "version": "2"}') == "@scope/widget"


def test_parse_conda_meta():
    assert parse_conda_meta("package:\n  name: nipype\n  version: 1.8") == "nipype"


def test_parse_cran_description():
    assert parse_cran_description("Package: admiral\nTitle: ADaM in R") == "admiral"


def test_parse_citation_cff_doi():
    d = parse_citation_cff("cff-version: 1.2.0\ntitle: Tool\ndoi: 10.5281/zenodo.12345")
    assert "10.5281/zenodo.12345" in d.doi


def test_parsers_tolerate_garbage():
    # A malformed or unrelated file must return None, never raise.
    assert parse_pyproject("not a toml file at all") is None
    assert parse_package_json("{broken json") is None
    assert parse_conda_meta("") is None


# ── De-duplication ─────────────────────────────────────────────────────────────


def test_discovered_dedupes():
    text = (
        "https://huggingface.co/acme/model https://huggingface.co/acme/model "  # same twice
        "10.1234/abc 10.1234/abc"
    )
    d = scan_text(text)
    assert d.hf == ["acme/model"]
    assert d.doi == ["10.1234/abc"]
