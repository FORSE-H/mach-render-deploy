"""Embeddings — the port for turning an entry into a vector, and the text that
represents an entry for semantic search.

`embedding_text` decides *what* about an entry gets embedded. It uses the
trusted content only — the seed's name and category plus harvested facts — never
the untrusted JSON-LD prose. As richer trusted fields arrive (a harvested
description, keywords), they are added here, in one place.

The Embedder port keeps the model choice (fastembed / all-MiniLM-L6-v2) behind
an interface, so the core and tests never import a model.
"""

from __future__ import annotations

from typing import Protocol

from core.catalog import Entry

EMBEDDING_DIM = 384


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one EMBEDDING_DIM-length vector per input text, in order."""
        ...


# Facts that carry semantic meaning for search. Numeric/status facts (stars,
# forks, downloads, archived) are deliberately excluded — a query like "federated
# learning" gains nothing from "9018", and such tokens only dilute the signal.
_SEMANTIC_FACTS = ("description",)


def embedding_text(entry: Entry) -> str:
    """The text that represents an entry for semantic search.

    Trusted content only, ordered by signal strength: the harvested description
    (richest), then name and category. Numeric facts are excluded on purpose.
    """
    facts = {f.field_name: f.value for f in entry.facts if f.value}
    parts = [entry.name]
    for key in _SEMANTIC_FACTS:
        if facts.get(key):
            parts.append(facts[key])
    parts.append(entry.category)
    return " | ".join(parts)
