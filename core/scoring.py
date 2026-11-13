"""Deterministic scoring — harvested facts to an Adopt / Assess / Caution ring.

MACH's differentiator: a judgement that is a pure function of measured signals,
not prose or an LLM. The ring and the human-readable reason are both derived
here from the facts, so the same inputs always yield the same verdict and the
reason always traces to the rules that fired.

Model: a few hard overrides (decisive facts like an archived repo or no licence),
then a points tally over maintenance, adoption, real usage, and licence quality.
Points map to a ring. Entries with no harvestable signal (data-sources, specs)
are not scored here — they stay curator-judged.

Thresholds are deliberately in one place so they can be reviewed and tuned as
one unit; each carries the rationale for why it sits where it does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from core.catalog import Entry
from core.facts import HarvestedField

Ring = str  # "Adopt" | "Assess" | "Caution"

# Bumped when the rules change, so a stored judgement records which rules produced
# it — the judgement's own provenance.
SCORER_VERSION = "1.0"
SCORER_SOURCE_ID = "mach_scorer"

# Licences recognised as permissive (easy to adopt) vs copyleft (usable, stricter).
_PERMISSIVE = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "0BSD"}
_COPYLEFT = {"GPL-3.0", "GPL-2.0", "AGPL-3.0", "LGPL-3.0", "MPL-2.0"}
_NO_LICENCE = {None, "", "NOASSERTION", "NONE", "OTHER"}


@dataclass(frozen=True)
class RuleHit:
    """One rule that fired, with its contribution and why — for the reason text."""

    signal: str
    detail: str
    points: int


@dataclass(frozen=True)
class Judgement:
    ring: Ring
    score: int
    reason: str
    hits: tuple[RuleHit, ...]
    scored: bool  # False = no harvestable signal, not auto-judged


def _months_since(timestamp: str | None, *, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - dt).days / 30.44


def _facts(entry: Entry) -> dict[str, str | None]:
    return {f.field_name: f.value for f in entry.facts}


# ── Repo-health indicator ─────────────────────────────────────────────────────
# A separate, transparent axis from the Adopt/Assess/Caution judgement: it
# answers "is this codebase alive and seriously maintained?" from the enriched
# signals, with a one-line reason that names what it looked at. Rules in one
# place, like the scorer, so the label always traces to why.
HEALTH_SOURCE_ID = "mach_health"


@dataclass(frozen=True)
class RepoHealth:
    status: str  # "Archived" | "Active" | "Maintained" | "Dormant" | "Stale" | None
    reason: str


def _truthy(value: str | None) -> bool:
    return str(value).lower() == "true"


def repo_health(entry: Entry, *, now: datetime | None = None) -> RepoHealth | None:
    """Rule-based health label, or None when there's no repo signal to judge.

    Archived wins outright. Otherwise recency sets the base (Active < 3mo,
    Maintained < 12mo, Dormant < 24mo, else Stale), and the presence of tests/CI
    is reported as corroborating evidence — it never upgrades a stale repo, only
    describes how serious the codebase looks.
    """
    now = now or datetime.now(timezone.utc)
    f = _facts(entry)
    last = f.get("repo_last_commit")
    if last is None and f.get("repo_stars") is None:
        return None  # not a repo-backed entry

    if _truthy(f.get("repo_archived")):
        return RepoHealth("Archived", "repository archived by its maintainer")

    months = _months_since(last, now=now)
    if months is None:
        return RepoHealth("Unknown", "no commit date available")

    if months < 3:
        status = "Active"
    elif months < 12:
        status = "Maintained"
    elif months < 24:
        status = "Dormant"
    else:
        status = "Stale"

    # Corroborating "serious codebase" signals (descriptive, not score-changing).
    marks = [name for name, key in (("tests", "has_tests"), ("CI", "has_ci"), ("releases", "has_releases"))
             if _truthy(f.get(key))]
    recency = (f"last commit ~{round(months)} mo ago" if months >= 1 else "committed within the last month")
    if marks:
        detail = f"{recency}; has {', '.join(marks)}"
    else:
        detail = f"{recency}; no tests/CI/releases detected"
    return RepoHealth(status, detail)


def health_fields(entry: Entry, *, now: datetime | None = None) -> list[HarvestedField]:
    """The repo-health label + reason as lineage-bearing facts, or none for a
    signal-less entry. Parallel to judgement_fields; its own source id."""
    now = now or datetime.now(timezone.utc)
    h = repo_health(entry, now=now)
    if h is None:
        return []

    def fld(name: str, value: str) -> HarvestedField:
        return HarvestedField(
            field_name=name, value=value, source_id=HEALTH_SOURCE_ID,
            raw_value="", harvested_at=now, fetch_status="ok",
        )

    return [fld("repo_health", h.status), fld("repo_health_reason", h.reason)]


def score_entry(entry: Entry, *, now: datetime | None = None) -> Judgement:
    now = now or datetime.now(timezone.utc)
    facts = _facts(entry)

    # No repo signals at all → editorial entry, not auto-scored.
    if not any(facts.get(k) for k in ("repo_stars", "repo_last_commit", "license")):
        return Judgement("Assess", 0, "Not auto-scored: no harvestable signals.", (), False)

    hits: list[RuleHit] = []
    licence = facts.get("license")

    # --- hard overrides (decisive) -------------------------------------------
    if str(facts.get("repo_archived")).lower() == "true":
        hit = RuleHit("maintenance", "repository archived by maintainer", -99)
        return _result("Caution", [hit], licence_capped=True)

    licence_missing = licence in _NO_LICENCE

    # --- points --------------------------------------------------------------
    hits += _maintenance_points(facts.get("repo_last_commit"), now)
    hits += _adoption_points(facts.get("repo_stars"))
    hits += _usage_points(facts.get("pypi_downloads"), facts.get("hf_downloads"))
    hits += _licence_points(licence, licence_missing)

    score = sum(h.points for h in hits)

    if licence_missing:
        # Can't recommend adopting something with no clear open licence.
        ring = "Caution" if score <= 0 else "Assess"
        return _result(ring, hits, licence_capped=True)

    if score >= 4:
        ring = "Adopt"
    elif score >= 1:
        ring = "Assess"
    else:
        ring = "Caution"
    return _result(ring, hits)


def _maintenance_points(last_commit: str | None, now: datetime) -> list[RuleHit]:
    months = _months_since(last_commit, now=now)
    if months is None:
        return []
    if months <= 3:
        return [RuleHit("maintenance", "committed within 3 months", 2)]
    if months <= 12:
        return [RuleHit("maintenance", f"last commit ~{months:.0f} months ago", 1)]
    if months <= 24:
        return [RuleHit("maintenance", f"last commit ~{months:.0f} months ago", 0)]
    return [RuleHit("maintenance", f"last commit ~{months:.0f} months ago (stale)", -2)]


def _adoption_points(stars: str | None) -> list[RuleHit]:
    n = _as_int(stars)
    if n is None:
        return []
    if n >= 1000:
        return [RuleHit("adoption", f"{n} stars (strong community)", 2)]
    if n >= 200:
        return [RuleHit("adoption", f"{n} stars", 1)]
    if n < 50:
        return [RuleHit("adoption", f"{n} stars (little traction)", -1)]
    return [RuleHit("adoption", f"{n} stars", 0)]


def _usage_points(pypi: str | None, hf: str | None) -> list[RuleHit]:
    best = max((v for v in (_as_int(pypi), _as_int(hf)) if v is not None), default=None)
    if best is None:
        return []
    if best >= 10000:
        return [RuleHit("usage", f"{best} monthly downloads (widely used)", 2)]
    if best >= 1000:
        return [RuleHit("usage", f"{best} monthly downloads", 1)]
    return [RuleHit("usage", f"{best} monthly downloads", 0)]


def _licence_points(licence: str | None, missing: bool) -> list[RuleHit]:
    if missing:
        return [RuleHit("licence", "no clear open licence", 0)]
    if licence in _PERMISSIVE:
        return [RuleHit("licence", f"permissive licence ({licence})", 1)]
    if licence in _COPYLEFT:
        return [RuleHit("licence", f"copyleft licence ({licence})", 0)]
    return [RuleHit("licence", f"licence {licence}", 0)]


def _result(ring: Ring, hits: list[RuleHit], *, licence_capped: bool = False) -> Judgement:
    score = sum(h.points for h in hits if h.points > -99)
    reason = _render_reason(ring, hits, licence_capped)
    return Judgement(ring, score, reason, tuple(hits), scored=True)


def _render_reason(ring: Ring, hits: list[RuleHit], licence_capped: bool) -> str:
    """Build the reason string purely from the rules that fired."""
    parts = [h.detail for h in hits if h.points != 0 or "licence" in h.signal]
    body = "; ".join(parts) if parts else "limited signals"
    cap = " Capped: no clear open licence." if licence_capped else ""
    return f"{ring} — {body}.{cap}"


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def judgement_fields(entry: Entry, *, now: datetime | None = None) -> list[HarvestedField]:
    """Score an entry and return its judgement as lineage-bearing facts.

    Only scored (repo-bearing) entries produce fields; editorial entries get
    none, so their judgement stays curator-set. The raw_value carries the full
    rule breakdown + scorer version, so the verdict is auditable.
    """
    now = now or datetime.now(timezone.utc)
    j = score_entry(entry, now=now)
    if not j.scored:
        return []

    raw = json.dumps(
        {
            "scorer_version": SCORER_VERSION,
            "score": j.score,
            "hits": [{"signal": h.signal, "detail": h.detail, "points": h.points} for h in j.hits],
        },
        separators=(",", ":"),
    )

    def fld(name: str, value: str) -> HarvestedField:
        return HarvestedField(
            field_name=name,
            value=value,
            source_id=SCORER_SOURCE_ID,
            raw_value=raw,
            harvested_at=now,
            fetch_status="ok",
        )

    return [fld("judgement", j.ring), fld("judgement_reason", j.reason)]
