"""Controlled vocabulary built from the audited catalogue.

Every canonical value comes from the live CatalogueEngine (workbook-derived,
audit-pinned). Nothing is invented. Aliases are a small, curated, deterministic
surface-form table; every alias resolves to a canonical catalogue value.

NO LLM, NO RAG, NO vector search in this module — it is the deterministic
grounding layer that constrains what the LLM may select.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.query_intent import CATALOGUE_UNITS, ReferenceQualifier

# ---------------------------------------------------------------------------
# Curated aliases (surface form -> canonical catalogue value).
# Grounded in the metric display names extracted from workbook QUESTION text.
# Every value here is verified to exist in the catalogue by tests.
# ---------------------------------------------------------------------------

METRIC_ALIASES: dict[str, str] = {
    # canonical display names (as used in workbook questions)
    "cascaded gain": "CGAIN",
    "cascade gain": "CGAIN",
    "stage power gain": "GAIN",
    "mismatch loss": "MismatchLoss",
    "desired channel power": "DCP",
    "channel power": "CP",
    "total node power": "TNP",
    "channel noise power": "CNP",
    "noise and distortion channel power": "NDCP",
    "cascaded compression": "CCOMP",
    "cascaded noise figure": "CNF",
    "carrier to noise and distortion ratio": "CNDR",
    "desired channel phase": "DCPH",
    "desired channel resistance": "DCR",
    "input 1 db compression": "IP1DB",
    "input saturation point": "IPSAT",
    "output 1 db compression": "OP1DB",
    "output saturation point": "OPSAT",
    # canonical codes (unambiguous short forms)
    "cgain": "CGAIN",
    "ccomp": "CCOMP",
    "cnp": "CNP",
    "cndr": "CNDR",
    "cnf": "CNF",
    "dcp": "DCP",
    "dcph": "DCPH",
    "dcr": "DCR",
    # NOTE: no bare "gain" alias on purpose — "gain" alone is ambiguous
    # between CGAIN (Cascaded Gain) and GAIN (Stage Power Gain) and MUST
    # produce a clarification, not a silent choice.
    "ip1db": "IP1DB",
    "ipsat": "IPSAT",
    "op1db": "OP1DB",
    "opsat": "OPSAT",
    "tnp": "TNP",
    "ndcp": "NDCP",
}

# Canonical request surface forms: exact words whose mapping to the
# workbook's REQUEST value is unambiguous. The workbook's canonical REQUEST
# values (Best / Worst / Minimum / Maximum / ...) are authoritative.
REQUEST_ALIASES: dict[str, str] = {
    "best": "Best",
    "worst": "Worst",
    "maximum": "Maximum",
    "minimum": "Minimum",
    "min": "Minimum",
    "max": "Maximum",
    "closest value": "Closest value",
    "closest": "Closest value",
    "range": "Range",
    "ripple": "Ripple",
    "threshold above": "Threshold Above",
    "threshold below": "Threshold Below",
    "above threshold": "Threshold Above",
    "below threshold": "Threshold Below",
    "part summary": "Part summary",
    "node summary": "Node summary",
    "particular frequency": "Particular frequency",
    "all frequencies": "All frequencies",
    "complete history": "Complete history",
    "average and median": "Average and median",
    "individual frequency best": "Individual frequency Best",
    "individual frequency worst": "Individual frequency Worst",
    "individual frequency maximum": "Individual frequency Maximum",
    "individual frequency minimum": "Individual frequency Minimum",
    "individual frequency final value": "Individual frequency final value",
    "individual frequency all nodes": "Individual frequency all nodes",
    "power difference": "Power difference",
    "power headroom": "Power headroom",
    "power separation": "Power separation",
    "input power margin": "Input power margin",
    "largest stage change": "Largest stage change",
    "stage power change": "Stage power change",
    "start-stop change": "Start-stop change",
}

# Natural-language variants whose mapping onto the workbook's canonical
# REQUEST values is NOT established by any explicit business rule.
# "lowest" could mean Minimum (smallest value) or Worst (worst-case gain);
# conflating them silently would corrupt catalogue semantics. These variants
# therefore produce NEEDS_CLARIFICATION with candidate REQUESTs, and the
# mapping decision is deferred to the office (REQUEST_VARIANTS_UNDER_REVIEW).
AMBIGUOUS_REQUEST_ALIASES: dict[str, tuple[str, ...]] = {
    "lowest": ("Minimum", "Worst"),
    "smallest": ("Minimum", "Worst"),
    "highest": ("Maximum", "Best"),
    "largest": ("Maximum", "Best"),
}

#: open semantic questions that require an explicit business rule from the
#: office before any automatic mapping is allowed.
REQUEST_VARIANTS_UNDER_REVIEW: tuple[dict[str, str], ...] = (
    {
        "variant": "lowest / smallest",
        "candidates": "Minimum vs Worst",
        "question": "Does 'lowest gain' mean the Minimum request (smallest "
                    "measured value) or the Worst request (worst-case run)?",
    },
    {
        "variant": "highest / largest",
        "candidates": "Maximum vs Best",
        "question": "Does 'highest gain' mean the Maximum request (largest "
                    "measured value) or the Best request (best-case run)?",
    },
)

#: qualifier surface form -> canonical ReferenceQualifier value
REFERENCE_QUALIFIER_ALIASES: dict[str, str] = {
    "channel power": ReferenceQualifier.CHANNEL_POWER.value,
    "desired channel power": ReferenceQualifier.DESIRED_CHANNEL_POWER.value,
    "channel noise power": ReferenceQualifier.CHANNEL_NOISE_POWER.value,
    "output-to-input": ReferenceQualifier.OUTPUT_TO_INPUT.value,
    "output to input": ReferenceQualifier.OUTPUT_TO_INPUT.value,
}

#: unit surface form -> canonical unit (exact case from the catalogue)
UNIT_ALIASES: dict[str, str] = {
    "db": "dB",
    "dbm": "dBm",
    "db20": "dB20",
    "ohm": "Ohm",
    "deg": "deg",
    "degree": "deg",
    "degrees": "deg",
}

FREQUENCY_ALIASES: dict[str, str] = {
    "all frequencies": "All Frequencies",
    "across frequency": "All Frequencies",
    "across all frequencies": "All Frequencies",
}


@dataclass
class ControlledVocabulary:
    """Catalogue-derived vocabularies exposed to the LLM and validators."""

    metrics: frozenset[str]
    requests: frozenset[str]
    scopes: frozenset[str]
    units: frozenset[str]
    frequency_selections: frozenset[str]
    reference_qualifiers: frozenset[str] = field(
        default_factory=lambda: frozenset(
            q.value for q in ReferenceQualifier if q is not ReferenceQualifier.NONE
        )
    )

    @classmethod
    def from_engine(cls, engine: CatalogueEngine) -> "ControlledVocabulary":
        """Build every vocabulary directly from the audited catalogue."""
        return cls(
            metrics=frozenset(engine.metrics()),
            requests=frozenset(engine.requests()),
            scopes=frozenset(engine.scopes()),
            units=frozenset(set(engine.units()) | CATALOGUE_UNITS),
            frequency_selections=frozenset(engine.freq_selections()),
        )

    # -- deterministic alias resolution (Stage A helpers) --------------------

    def resolve_metric(self, surface: str) -> str | None:
        return METRIC_ALIASES.get(surface.strip().lower())

    def match_metric_alias(self, text: str) -> tuple[str, str] | None:
        """Authoritative metric identification by exact alias phrase.

        Longest-match strategy: collect every alias that occurs in the text
        with word boundaries, keep only the LONGEST matched alias(es), and
        return (canonical, alias) when exactly one canonical value remains.
        This makes "desired channel power" resolve to DCP (not CP) and
        "channel noise power" to CNP (not CP), regardless of substring
        overlap between aliases.

        Returns None when nothing matches or the longest matches are
        ambiguous (multiple different canonical values at the same length).
        """
        text_lower = text.lower()
        matches: list[tuple[str, str]] = []  # (alias, canonical)
        for alias, canonical in METRIC_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text_lower):
                matches.append((alias, canonical))
        if not matches:
            return None
        longest = max(len(a) for a, _ in matches)
        best = {canonical for alias, canonical in matches
                if len(alias) == longest}
        if len(best) == 1:
            alias = next(a for a, c in matches if c == best.copy().pop()
                         and len(a) == longest)
            return best.pop(), alias
        return None

    def resolve_request(self, surface: str) -> str | None:
        return REQUEST_ALIASES.get(surface.strip().lower())

    def resolve_unit(self, surface: str) -> str | None:
        return UNIT_ALIASES.get(surface.strip().lower())

    def resolve_frequency(self, surface: str) -> str | None:
        return FREQUENCY_ALIASES.get(surface.strip().lower())

    def resolve_reference_qualifier(self, surface: str) -> str | None:
        return REFERENCE_QUALIFIER_ALIASES.get(surface.strip().lower())

    def candidates_for_metric(self, text_lower: str) -> list[str]:
        """Canonical metrics plausibly meant by the text.

        Longest-match-aware: aliases subsumed by a longer matched alias are
        not counted as separate candidates (so "desired channel power"
        yields only DCP, not DCP and CP).
        """
        matched = [(alias, canonical) for alias, canonical in METRIC_ALIASES.items()
                   if re.search(rf"\b{re.escape(alias)}\b", text_lower)]
        if not matched:
            return []
        longest = max(len(a) for a, _ in matched)
        best = {canonical for alias, canonical in matched if len(alias) == longest}
        return sorted(best & set(self.metrics))

    def validate_dimension(self, dimension: str, value: str) -> bool:
        allowed = {
            "metric": self.metrics,
            "request": self.requests,
            "scope": self.scopes,
            "unit": self.units,
            "frequency_selection": self.frequency_selections,
            "reference_qualifier": self.reference_qualifiers,
        }.get(dimension)
        if allowed is None:
            raise ValueError(f"unknown dimension {dimension!r}")
        return value in allowed
