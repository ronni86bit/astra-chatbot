"""Canonical QueryIntent model: the structured query representation.

This module defines the canonical structured query that a future LLM must
produce for deterministic catalogue lookup. It is PURE VALIDATION AND
GRAMMAR — no LLM, no RAG, no vector search, no semantic retrieval.

Canonical query representation (extends the audited 5-field base key):

    metric, request, scope, frequency_selection, unit  +  params

`params` is a request-aware structured object (never a free-form string):

    request                                   required params
    ----------------------------------------  ---------------------------
    Threshold Above / Threshold Below         ThresholdParams(threshold)
    Closest value                             ClosestTargetParams(closest_target)
    Range                                     RangeParams(range_min, range_max)
    Power difference / Individual frequency   ReferenceQualifierParams(
        power difference                          reference_qualifier)
    all other catalogue requests              NoParams()

GRAMMAR PROVENANCE (requirement 6): every regex and enum value below was
derived programmatically from the actual workbook QUESTION text and verified:
- 497/497 parameterised-family rows parse (0 failures)
- QUESTION-embedded unit matches the UNIT column in 377/377 rows
- QUESTION direction ("above"/"below") matches the request in 377/377 rows
- 5-field key + parsed params = 4,768 unique keys for 4,768 rows (0 duplicates)

Reference-qualifier wording is preserved VERBATIM from the workbook
(requirement 7); business meanings that the workbook does not state are
listed in OFFICE_REVIEW_ITEMS and are NOT asserted here.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Controlled vocabularies (audit-derived, pinned by tests)
# ---------------------------------------------------------------------------

#: Units observed in the workbook UNIT column (verified against the engine
#: catalogue by tests). Validation is exact and case-sensitive.
CATALOGUE_UNITS: frozenset[str] = frozenset({"dB", "dBm", "dB20", "deg", "Ohm"})

#: All request types observed in the workbook REQUEST column.
KNOWN_REQUESTS: frozenset[str] = frozenset({
    "All frequencies",
    "Average and median",
    "Best",
    "Closest value",
    "Complete history",
    "Individual frequency Best",
    "Individual frequency Maximum",
    "Individual frequency Minimum",
    "Individual frequency Worst",
    "Individual frequency all nodes",
    "Individual frequency final value",
    "Individual frequency input margin",
    "Individual frequency largest stage change",
    "Individual frequency power difference",
    "Individual frequency power headroom",
    "Individual frequency power separation",
    "Individual frequency stage change",
    "Input power margin",
    "Largest stage change",
    "Maximum",
    "Minimum",
    "Node summary",
    "Part summary",
    "Particular frequency",
    "Power difference",
    "Power headroom",
    "Power separation",
    "Range",
    "Ripple",
    "Stage power change",
    "Start-stop change",
    "Threshold Above",
    "Threshold Below",
    "Worst",
})

#: Requests that carry structured parameters -> their required params type.
#: Every other catalogue request REQUIRES NoParams (irrelevant params rejected).
REQUEST_PARAM_TYPES: dict[str, type["QueryParams"]] = {}  # filled below


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuestionParamParseError(ValueError):
    """A QUESTION text could not be parsed with the known grammar.

    The verbatim question text is preserved in the message. Such rows are
    NEVER guessed; they are reported for office confirmation.
    """


# ---------------------------------------------------------------------------
# Reference qualifier — verbatim-controlled (requirement 7)
# ---------------------------------------------------------------------------


class ReferenceQualifier(str, Enum):
    """Reference quantity/phrase in power-difference questions.

    Values are the exact phrases observed in workbook QUESTION text.
    Business semantics (e.g. what 'desired' physically denotes) are NOT
    asserted here — see OFFICE_REVIEW_ITEMS.
    """

    #: "Show total node power above channel power ..." (TNP)
    CHANNEL_POWER = "channel power"
    #: "Show total node power above desired channel power ..." (TNP)
    DESIRED_CHANNEL_POWER = "desired channel power"
    #: "Show noise plus distortion power above channel noise power ..." (CNDR)
    CHANNEL_NOISE_POWER = "channel noise power"
    #: "Show output-to-input P1dB difference ..." (OP1DB) / saturation (IPSAT)
    OUTPUT_TO_INPUT = "output-to-input"
    #: No reference qualifier (placeholder for non-difference queries)
    NONE = "none"


#: Phrases whose business meaning must be confirmed by the office.
#: The model preserves the wording and does NOT guess semantics.
OFFICE_REVIEW_ITEMS: tuple[dict[str, str], ...] = (
    {
        "item": "desired channel power vs channel power",
        "observed_wording": ("Show total node power above desired channel power ...",
                             "Show total node power above channel power ..."),
        "question": "What physically distinguishes the 'desired' channel power "
                    "from the plain channel power measurement?",
    },
    {
        "item": "channel noise power reference (CNDR)",
        "observed_wording": ("Show noise plus distortion power above channel noise power ...",),
        "question": "Confirm 'channel noise power' is the intended reference "
                    "quantity for noise-plus-distortion differences.",
    },
    {
        "item": "output-to-input difference direction (OP1DB / IPSAT)",
        "observed_wording": ("Show output-to-input P1dB difference ...",
                             "Show output-to-input saturation difference ..."),
        "question": "Confirm whether 'output-to-input' should be modelled as a "
                    "reference qualifier or as an explicit direction pair.",
    },
    {
        "item": "subtraction semantics (CP)",
        "observed_wording": ("Show channel power minus desired channel power ...",),
        "question": "Confirm the canonical operand order (minuend/subtrahend) "
                    "this wording represents.",
    },
)


# ---------------------------------------------------------------------------
# Parameter objects (frozen; extra fields forbidden; strict numerics)
# ---------------------------------------------------------------------------


def _as_finite_number(value) -> float:
    """Accept int/float; reject bool and strings (strict numerics).

    Raises ValueError (not TypeError) so pydantic collects it as a
    ValidationError.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"numeric parameter required (int or float), got "
            f"{type(value).__name__}: {value!r}"
        )
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"numeric parameter must be finite, got {value!r}")
    return value


class QueryParams(BaseModel):
    """Base class for request-specific parameter objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def key(self) -> tuple:
        """Hashable discriminator tuple (the effective 6th key dimension)."""
        raise NotImplementedError


class NoParams(QueryParams):
    """Parameter object for requests that carry no extra parameters."""

    def key(self) -> tuple:
        return ("none",)


class ThresholdParams(QueryParams):
    """Threshold value for Threshold Above / Threshold Below requests.

    The comparison direction is carried by the request itself
    ("Threshold Above" vs "Threshold Below"), not duplicated here.
    """

    threshold: Annotated[float, Field(description="Numeric threshold bound")]

    _norm_threshold = field_validator("threshold", mode="before")(
        lambda v: _as_finite_number(v)
    )

    def key(self) -> tuple:
        return ("threshold", self.threshold)


class ClosestTargetParams(QueryParams):
    """Target value for Closest value requests."""

    closest_target: Annotated[float, Field(description="Numeric closest-match target")]

    _norm_target = field_validator("closest_target", mode="before")(
        lambda v: _as_finite_number(v)
    )

    def key(self) -> tuple:
        return ("closest", self.closest_target)


class RangeParams(QueryParams):
    """Lower/upper bounds for Range requests."""

    range_min: Annotated[float, Field(description="Inclusive lower bound")]
    range_max: Annotated[float, Field(description="Inclusive upper bound")]

    _norm_min = field_validator("range_min", mode="before")(
        lambda v: _as_finite_number(v)
    )
    _norm_max = field_validator("range_max", mode="before")(
        lambda v: _as_finite_number(v)
    )

    @model_validator(mode="after")
    def _min_below_max(self) -> "RangeParams":
        if self.range_min >= self.range_max:
            raise ValueError(
                f"range_min must be strictly less than range_max "
                f"(got {self.range_min} >= {self.range_max})"
            )
        return self

    def key(self) -> tuple:
        return ("range", self.range_min, self.range_max)


class ReferenceQualifierParams(QueryParams):
    """Controlled reference qualifier for power-difference requests."""

    reference_qualifier: ReferenceQualifier

    @model_validator(mode="after")
    def _not_none(self) -> "ReferenceQualifierParams":
        if self.reference_qualifier is ReferenceQualifier.NONE:
            raise ValueError(
                "reference_qualifier must be a concrete qualifier "
                f"({[q.value for q in ReferenceQualifier if q is not ReferenceQualifier.NONE]}); "
                "'none' is only valid via NoParams"
            )
        return self

    def key(self) -> tuple:
        return ("refqual", self.reference_qualifier.value)


QueryParamsTypes = Union[
    ThresholdParams,
    ClosestTargetParams,
    RangeParams,
    ReferenceQualifierParams,
    NoParams,
]

REQUEST_PARAM_TYPES.update({
    "Threshold Above": ThresholdParams,
    "Threshold Below": ThresholdParams,
    "Closest value": ClosestTargetParams,
    "Range": RangeParams,
    "Power difference": ReferenceQualifierParams,
    "Individual frequency power difference": ReferenceQualifierParams,
})


# ---------------------------------------------------------------------------
# QueryIntent
# ---------------------------------------------------------------------------


class QueryIntent(BaseModel):
    """Canonical structured query: 5 audited base dimensions + params.

    This is the contract the future LLM must satisfy. Validation is strict:
    - unit must be a catalogue unit (exact string)
    - request must be a known catalogue request
    - params type must match the request (missing -> error, irrelevant -> error)
    - numeric params accept only int/float (no bool, no numeric strings)
    - reference qualifiers are enum-controlled
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    metric: str = Field(min_length=1)
    request: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    frequency_selection: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    params: QueryParamsTypes = Field(default_factory=NoParams)

    @field_validator("params", mode="before")
    @classmethod
    def _none_params_to_empty(cls, v):
        """Normalise explicit None to NoParams before union validation."""
        return NoParams() if v is None else v

    @field_validator("unit")
    @classmethod
    def _unit_in_catalogue(cls, v: str) -> str:
        if v not in CATALOGUE_UNITS:
            raise ValueError(
                f"unit {v!r} is not a catalogue unit {sorted(CATALOGUE_UNITS)}"
            )
        return v

    @field_validator("request")
    @classmethod
    def _request_known(cls, v: str) -> str:
        if v not in KNOWN_REQUESTS:
            raise ValueError(
                f"request {v!r} is not a known catalogue request"
            )
        return v

    @model_validator(mode="after")
    def _request_aware_params(self) -> "QueryIntent":
        required_type = REQUEST_PARAM_TYPES.get(self.request, NoParams)
        # None / omitted params normalise to NoParams
        if self.params is None:
            object.__setattr__(self, "params", NoParams())
        params = self.params
        if isinstance(params, NoParams) and required_type is not NoParams:
            raise ValueError(
                f"request {self.request!r} requires "
                f"{required_type.__name__} params, got none"
            )
        if not isinstance(params, NoParams) and required_type is NoParams:
            raise ValueError(
                f"request {self.request!r} takes no parameters "
                f"(got {type(params).__name__})"
            )
        if not isinstance(params, NoParams) and not isinstance(params, required_type):
            raise ValueError(
                f"request {self.request!r} requires "
                f"{required_type.__name__} params, got {type(params).__name__}"
            )
        return self

    def base_key(self) -> tuple[str, str, str, str, str]:
        """The audited 5-field base key (canonical order)."""
        return (self.metric, self.request, self.scope,
                self.frequency_selection, self.unit)

    def intent_key(self) -> tuple:
        """Full lookup key: 5-field base key + structured params key."""
        return self.base_key() + self.params.key()


# ---------------------------------------------------------------------------
# Deterministic QUESTION-text grammar (regex, no ML)
# ---------------------------------------------------------------------------

_NUM = r"(-?\d+(?:\.\d+)?)"          # includes negative values, e.g. -40
_UNIT = r"(dB20|dBm|dB|deg|Ohm)"     # workbook-observed unit spellings

RE_THRESHOLD = re.compile(
    rf"\b(?P<direction>above|below)\s+(?P<value>{_NUM[1:-1]})\s*(?P<unit>{_UNIT[1:-1]})\b",
    re.IGNORECASE,
)
RE_CLOSEST = re.compile(
    rf"\bclosest\s+to\s+(?P<value>{_NUM[1:-1]})\s*(?P<unit>{_UNIT[1:-1]})\b",
    re.IGNORECASE,
)
RE_RANGE = re.compile(
    rf"\bbetween\s+(?P<lo>{_NUM[1:-1]})\s+and\s+(?P<hi>{_NUM[1:-1]})\s*(?P<unit>{_UNIT[1:-1]})\b",
    re.IGNORECASE,
)
RE_DESIRED_REF = re.compile(r"\babove\s+desired\s+channel\s+power\b", re.IGNORECASE)
RE_PLAIN_REF = re.compile(r"\babove\s+channel\s+power\b", re.IGNORECASE)
RE_NOISE_REF = re.compile(r"\babove\s+channel\s+noise\s+power\b", re.IGNORECASE)
RE_MINUS_REF = re.compile(r"\bminus\s+desired\s+channel\s+power\b", re.IGNORECASE)
RE_OUTPUT_TO_INPUT = re.compile(r"^show\s+output-to-input\b", re.IGNORECASE)

_FAMILY_REQUESTS = frozenset(REQUEST_PARAM_TYPES)


def parse_question_params(
    request: str,
    question: str,
    unit: str | None = None,
) -> QueryParams:
    """Deterministically extract request-specific params from QUESTION text.

    Args:
        request: The catalogue REQUEST value (drives the grammar choice).
        question: The verbatim QUESTION text.
        unit: Optional UNIT-column value; when given, the unit embedded in
            the question is cross-checked against it (audit-verified
            invariant: 0 mismatches in 377 family rows).

    Returns:
        The structured params object (NoParams for non-family requests).

    Raises:
        QuestionParamParseError: unknown wording, unit mismatch, direction
            mismatch, or malformed numeric grammar. The verbatim question is
            preserved in the message. Never guesses.
    """
    if request not in _FAMILY_REQUESTS:
        return NoParams()

    def fail(reason: str) -> None:
        raise QuestionParamParseError(
            f"Cannot parse params for request={request!r}: {reason}. "
            f"QUESTION (verbatim): {question!r}"
        )

    if request in ("Threshold Above", "Threshold Below"):
        m = RE_THRESHOLD.search(question)
        if not m:
            fail("no 'above/below <number> <unit>' threshold pattern found")
        expected_dir = "above" if request == "Threshold Above" else "below"
        if m.group("direction").lower() != expected_dir:
            fail(f"question direction {m.group('direction')!r} contradicts request")
        if unit is not None and m.group("unit") != unit:
            fail(f"question unit {m.group('unit')!r} != catalogue unit {unit!r}")
        return ThresholdParams(threshold=float(m.group("value")))

    if request == "Closest value":
        m = RE_CLOSEST.search(question)
        if not m:
            fail("no 'closest to <number> <unit>' pattern found")
        if unit is not None and m.group("unit") != unit:
            fail(f"question unit {m.group('unit')!r} != catalogue unit {unit!r}")
        return ClosestTargetParams(closest_target=float(m.group("value")))

    if request == "Range":
        m = RE_RANGE.search(question)
        if not m:
            fail("no 'between <lo> and <hi> <unit>' pattern found")
        lo, hi = float(m.group("lo")), float(m.group("hi"))
        if lo >= hi:
            fail(f"range bounds not increasing (lo={lo}, hi={hi})")
        if unit is not None and m.group("unit") != unit:
            fail(f"question unit {m.group('unit')!r} != catalogue unit {unit!r}")
        return RangeParams(range_min=lo, range_max=hi)

    # Power difference / Individual frequency power difference
    # Order matters: check the 'desired' phrase before the plain phrase.
    if RE_DESIRED_REF.search(question):
        return ReferenceQualifierParams(
            reference_qualifier=ReferenceQualifier.DESIRED_CHANNEL_POWER)
    if RE_PLAIN_REF.search(question):
        return ReferenceQualifierParams(
            reference_qualifier=ReferenceQualifier.CHANNEL_POWER)
    if RE_NOISE_REF.search(question):
        return ReferenceQualifierParams(
            reference_qualifier=ReferenceQualifier.CHANNEL_NOISE_POWER)
    if RE_MINUS_REF.search(question):
        # CP family: "channel power minus desired channel power".
        # Reference (subtrahend) wording preserved; semantics under office review.
        return ReferenceQualifierParams(
            reference_qualifier=ReferenceQualifier.DESIRED_CHANNEL_POWER)
    if RE_OUTPUT_TO_INPUT.search(question):
        return ReferenceQualifierParams(
            reference_qualifier=ReferenceQualifier.OUTPUT_TO_INPUT)
    fail("no known reference-qualifier phrase found")


def intent_from_row(row) -> QueryIntent:
    """Build a QueryIntent from a catalogue QARow (duck-typed).

    Parses the row's QUESTION text to obtain its structured parameters.
    """
    params = parse_question_params(row.request, row.question, row.unit)
    return QueryIntent(
        metric=row.metric,
        request=row.request,
        scope=row.scope,
        frequency_selection=row.frequency_selection,
        unit=row.unit,
        params=params,
    )
