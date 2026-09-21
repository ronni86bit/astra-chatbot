"""Stage A: deterministic normalization of obvious surface forms.

Deliberately SMALL and explicit (per milestone constraint — no enormous
rule engine). Handles only unambiguous mechanical rewrites:

- whitespace collapsing / trimming
- GHz -> MHz conversion ("2.5 GHz" -> "2500 MHz")
- unit casing ("dbm" -> "dBm")
- "all frequencies" casing ("All Frequencies")
- exact known alias phrases (metric / request / qualifier vocabularies)

Semantic language is Stage B's job (LLM structured interpretation).
"""

from __future__ import annotations

import re

from rf_catalogue.nlp.vocabulary import ControlledVocabulary

_WHITESPACE = re.compile(r"\s+")
_GHZ = re.compile(r"(\d+(?:\.\d+)?)\s*ghz\b", re.IGNORECASE)
_MHZ = re.compile(r"(\d+(?:\.\d+)?)\s*mhz\b", re.IGNORECASE)


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def ghz_to_mhz(text: str) -> str:
    """Rewrite GHz frequencies as catalogue-style MHz values."""

    def _sub(m: re.Match) -> str:
        value = float(m.group(1)) * 1000.0
        return f"{int(round(value))} MHz"

    return _GHZ.sub(_sub, text)


def normalize_mhz_spacing(text: str) -> str:
    """Normalize '2500mhz' / '2500 mhz' -> '2500 MHz'."""
    return _MHZ.sub(lambda m: f"{int(float(m.group(1)))} MHz", text)


def normalize_units_casing(text: str) -> str:
    """Canonical unit casing on standalone tokens (dB, dBm, dB20, Ohm, deg)."""
    text = re.sub(r"\bdb20\b", "dB20", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdbm\b", "dBm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdB\b", "dB", text, flags=re.IGNORECASE)
    text = re.sub(r"\bohm\b", "Ohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdegrees?\b", "deg", text, flags=re.IGNORECASE)
    return text


class Normalizer:
    """Stage A pipeline: mechanical rewrites only, no semantics."""

    def __init__(self, vocabulary: ControlledVocabulary):
        self.vocab = vocabulary

    def normalize(self, text: str) -> str:
        text = collapse_whitespace(text)
        text = ghz_to_mhz(text)
        text = normalize_mhz_spacing(text)
        text = normalize_units_casing(text)
        # Exact-case canonicalisation of catalogue constants.
        text = re.sub(r"\ball frequencies\b", "All Frequencies", text, flags=re.IGNORECASE)
        text = re.sub(r"\bfinal node\b", "Final node", text, flags=re.IGNORECASE)
        text = re.sub(r"\ball nodes\b", "All nodes", text, flags=re.IGNORECASE)
        return text
