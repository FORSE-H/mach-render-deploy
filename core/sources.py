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
        source_id="pypi",
        name="PyPI download stats (pypistats.org)",
        base_url="https://pypistats.org",
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
        source_id="curator",
        name="MACH editorial",
        data_licence="CC-BY-4.0",
        attribution_required=True,
        attribution_text="MACH — Machine-Actionable Catalog for Healthcare (CC-BY-4.0)",
    ),
)
