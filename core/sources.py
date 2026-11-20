"""Authoritative sources — the registry that gives lineage its meaning.

Every harvested fact points at a source_id; this is where those ids are defined,
together with the licence and attribution each source requires. Storing them once
lets the API and exports render correct credit for any field's provenance.

This is small, stable reference data, so it lives in the core as data rather than
in a config file. New sources are added here as adapters for them are written.
"""

from __future__ import annotations

from pydantic import BaseModel


class MetadataSource(BaseModel):
    source_id: str
    name: str
    base_url: str | None = None
    data_licence: str | None = None
    attribution_required: bool = False
    attribution_text: str | None = None


SOURCES: tuple[MetadataSource, ...] = (
    MetadataSource(
        source_id="github_api",
        name="GitHub REST API",
        base_url="https://api.github.com",
        data_licence="GitHub Terms of Service",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="gitlab_api",
        name="GitLab REST API",
        base_url="https://gitlab.com/api/v4",
        data_licence="GitLab Terms of Service",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="codeberg_api",
        name="Codeberg (Gitea) API",
        base_url="https://codeberg.org/api/v1",
        data_licence="Codeberg Terms of Use",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="bitbucket_api",
        name="Bitbucket REST API",
        base_url="https://api.bitbucket.org/2.0",
        data_licence="Bitbucket Terms of Service",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="pypi",
        name="PyPI download stats (pypistats.org)",
        base_url="https://pypistats.org",
        data_licence="public",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="discovery",
        name="MACH identifier discovery (README/manifests/badges/CITATION.cff)",
        data_licence="derived from each repo's own files",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="conda",
        name="conda-forge downloads (anaconda.org)",
        base_url="https://anaconda.org",
        data_licence="public",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="npm",
        name="npm download stats (api.npmjs.org)",
        base_url="https://www.npmjs.com",
        data_licence="public",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="cran",
        name="CRAN download stats (cranlogs.r-pkg.org)",
        base_url="https://cran.r-project.org",
        data_licence="public",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="huggingface",
        name="Hugging Face Hub API",
        base_url="https://huggingface.co",
        data_licence="Hugging Face Terms of Service",
        attribution_required=False,
    ),
    MetadataSource(
        source_id="mach_scorer",
        name="MACH deterministic scorer",
        data_licence="CC-BY-4.0",
        attribution_required=False,
        attribution_text="Algorithmic judgement from verified signals (MACH scorer).",
    ),
    MetadataSource(
        source_id="curator",
        name="MACH editorial",
        data_licence="CC-BY-4.0",
        attribution_required=True,
        attribution_text="MACH — Machine-Actionable Catalog for Healthcare (CC-BY-4.0)",
    ),
)
