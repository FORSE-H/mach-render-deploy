"""discovery — extract external identifiers from a repo's own files.

The harvester's registry/citation sources can only enrich an entry if they know
its identifier (a PyPI/conda/npm package name, a Hugging Face id, a DOI). Those
are rarely in the minimal seed, so most entries went un-enriched. This module
reads a repo's README, package manifests, badge URLs, and CITATION.cff and
extracts those identifiers, so the downstream sources fire without a human
hand-typing each fact.

It is deliberately *pure*: every function takes already-fetched text and returns
what it found. The HTTP fetching lives in the adapter (adapters/discovery_client.py),
so this logic is fully testable with no network. Confidence is honest — a
discovered id is only accepted when it has the right shape (e.g. an `org/name`
Hugging Face reference, not a bare org or collection page); anything ambiguous is
dropped rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Discovered-identifier container ───────────────────────────────────────────


@dataclass
class Discovered:
    """What a scan of a repo's files turned up. Lists are de-duplicated, order-
    preserving. Empty means 'nothing found', never 'not checked'."""

    pypi: list[str] = field(default_factory=list)
    conda: list[str] = field(default_factory=list)
    npm: list[str] = field(default_factory=list)
    cran: list[str] = field(default_factory=list)
    hf: list[str] = field(default_factory=list)          # "org/name" model/dataset ids
    doi: list[str] = field(default_factory=list)

    def _add(self, bucket: list[str], value: str | None) -> None:
        if value:
            v = value.strip()
            if v and v not in bucket:
                bucket.append(v)

    def merge(self, other: "Discovered") -> "Discovered":
        for name in ("pypi", "conda", "npm", "cran", "hf", "doi"):
            for v in getattr(other, name):
                self._add(getattr(self, name), v)
        return self


# ── Patterns ──────────────────────────────────────────────────────────────────

# A Hugging Face model/dataset reference: huggingface.co/[models|datasets/]org/name.
# We reject bare org pages and /collections/ (not a single artifact).
_HF_RE = re.compile(
    r"huggingface\.co/(?:models/|datasets/)?([A-Za-z0-9][\w.-]+/[\w.-]+)", re.I
)
_HF_REJECT = re.compile(r"huggingface\.co/(?:collections|organizations|settings)/", re.I)

# A DOI, from a doi.org URL or a bare "10.xxxx/..." token.
_DOI_URL_RE = re.compile(r"(?:doi\.org/|dx\.doi\.org/)(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.I)
_DOI_BARE_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.I)

# Shields.io / badge URLs that encode a package name.
_BADGE_PYPI_RE = re.compile(r"pypi/[a-z]+/([A-Za-z0-9._-]+)", re.I)
_BADGE_NPM_RE = re.compile(r"npm/[a-z]+/((?:@[\w.-]+/)?[\w.-]+)", re.I)
_BADGE_CONDA_RE = re.compile(r"conda/[a-z]+/[\w.-]+/([\w.-]+)", re.I)

# A trailing segment that is noise, not a package name.
_NOISE_NAMES = {"badge", "shield", "version", "status", "none", "", "latest"}


def _clean_doi(doi: str | None) -> str | None:
    """Trim trailing punctuation and badge image suffixes from a DOI."""
    if not doi:
        return None
    d = doi.rstrip(").,;")
    d = re.sub(r"\.(svg|png|json|jpg)$", "", d)
    return d or None


def _clean(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().strip("/").lower()
    # Badge URLs often end in an image extension (shields.io: .../pydicom.svg);
    # strip a trailing image suffix so the package name is clean.
    n = re.sub(r"\.(svg|png|json|jpg)$", "", n)
    return None if n in _NOISE_NAMES else n


# ── README / free-text scanning ───────────────────────────────────────────────


def scan_text(text: str) -> Discovered:
    """Find HF ids, DOIs, and badge-encoded package names in free text (a README).
    Only well-shaped references are kept; bare org pages and collections are dropped."""
    out = Discovered()
    if not text:
        return out

    for m in _HF_RE.finditer(text):
        # Skip if this match sits inside a rejected path (collections/org page).
        start = m.start()
        window = text[max(0, start - 20): start + 1]
        if _HF_REJECT.search(window + m.group(0)):
            continue
        ref = m.group(1).rstrip("/").rstrip(").,")
        # Must look like org/name (exactly one slash in the captured ref).
        if ref.count("/") == 1 and not ref.endswith("/"):
            out._add(out.hf, ref)

    for m in _DOI_URL_RE.finditer(text):
        out._add(out.doi, _clean_doi(m.group(1)))
    # Bare DOIs too (common in CITATION.cff / badges), de-duped against the above.
    for m in _DOI_BARE_RE.finditer(text):
        out._add(out.doi, _clean_doi(m.group(1)))

    for m in _BADGE_PYPI_RE.finditer(text):
        out._add(out.pypi, _clean(m.group(1)))
    for m in _BADGE_NPM_RE.finditer(text):
        out._add(out.npm, _clean(m.group(1)))
    for m in _BADGE_CONDA_RE.finditer(text):
        out._add(out.conda, _clean(m.group(1)))

    return out


# ── Manifest parsing (declared package names are authoritative) ────────────────


def parse_pyproject(text: str) -> str | None:
    """`[project] name = "..."` or Poetry `[tool.poetry] name = "..."`."""
    if not text:
        return None
    # tomllib would be cleaner, but a targeted regex avoids a hard dep and tolerates
    # partial/odd files. Match a top-level name under [project] or [tool.poetry].
    m = re.search(r'(?mi)^\s*name\s*=\s*["\']([A-Za-z0-9._-]+)["\']', text)
    return _clean(m.group(1)) if m else None


def parse_setup_py(text: str) -> str | None:
    """`setup(name="...")` in a legacy setup.py."""
    if not text:
        return None
    m = re.search(r'name\s*=\s*["\']([A-Za-z0-9._-]+)["\']', text)
    return _clean(m.group(1)) if m else None


def parse_package_json(text: str) -> str | None:
    """npm package name from package.json (quoted, supports @scope/name)."""
    if not text:
        return None
    m = re.search(r'"name"\s*:\s*"((?:@[\w.-]+/)?[\w.-]+)"', text)
    return _clean(m.group(1)) if m else None


def parse_conda_meta(text: str) -> str | None:
    """conda-forge recipe name from meta.yaml (`package:` → `name:`)."""
    if not text:
        return None
    m = re.search(r'(?mi)^\s*name:\s*["\']?([A-Za-z0-9._-]+)', text)
    return _clean(m.group(1)) if m else None


def parse_cran_description(text: str) -> str | None:
    """CRAN/Bioconductor package name from the DESCRIPTION file (`Package: ...`)."""
    if not text:
        return None
    m = re.search(r'(?mi)^\s*Package:\s*([A-Za-z0-9._-]+)', text)
    return _clean(m.group(1)) if m else None


def parse_citation_cff(text: str) -> Discovered:
    """A CITATION.cff file — extract a DOI if one is declared (doi: or identifiers)."""
    out = Discovered()
    if not text:
        return out
    m = re.search(r'(?mi)^\s*doi:\s*["\']?(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', text)
    if m:
        out._add(out.doi, m.group(1).rstrip(")\"'.,"))
    # Also scan for any bare DOI in the identifiers block.
    out.merge(Discovered(doi=[d for d in _scan_bare_dois(text)]))
    return out


def _scan_bare_dois(text: str) -> list[str]:
    seen: list[str] = []
    for m in _DOI_BARE_RE.finditer(text or ""):
        v = _clean_doi(m.group(1))
        if v and v not in seen:
            seen.append(v)
    return seen


# ── The file → parser map (used by the adapter) ───────────────────────────────

# Filename (lowercased basename) → a function that returns a single package name.
MANIFEST_PARSERS: dict[str, tuple[str, object]] = {
    "pyproject.toml": ("pypi", parse_pyproject),
    "setup.py": ("pypi", parse_setup_py),
    "setup.cfg": ("pypi", parse_pyproject),   # [metadata] name = ... matches the same regex
    "package.json": ("npm", parse_package_json),
    "meta.yaml": ("conda", parse_conda_meta),
    "description": ("cran", parse_cran_description),  # R's DESCRIPTION (case-insensitive match)
}
