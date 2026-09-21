"""Structured-output schemas for the LLM and the parser's outcome states.

QueryIntentDraft is the ONLY shape the LLM is allowed to emit: a strict
JSON-Schema-constrained object whose dimension fields are enums drawn from
the controlled catalogue vocabulary. There is no free-form text channel —
anything the schema cannot express cannot be produced.

ParseOutcome is the parser's contract with the service layer:
RESOLVED / NEEDS_CLARIFICATION / NOT_FOUND / PARSE_ERROR.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from rf_catalogue.query_intent import QueryIntent

# ---------------------------------------------------------------------------
# LLM draft models (all fields optional; enums constrain the vocabulary)
# ---------------------------------------------------------------------------


class ReferenceQualifierDraft(str, Enum):
    channel_power = "channel power"
    desired_channel_power = "desired channel power"
    channel_noise_power = "channel noise power"
    output_to_input = "output-to-input"


class ThresholdParamsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float


class ClosestTargetParamsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    closest_target: float


class RangeParamsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range_min: float
    range_max: float


class ReferenceQualifierParamsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_qualifier: ReferenceQualifierDraft


class NoParamsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "none"


ParamsDraft = Union[
    ThresholdParamsDraft,
    ClosestTargetParamsDraft,
    RangeParamsDraft,
    ReferenceQualifierParamsDraft,
    NoParamsDraft,
]


class QueryIntentDraft(BaseModel):
    """Everything-optional mirror of QueryIntent for LLM structured output.

    Null dimensions mean "not determinable from the user text" — the parser
    turns them into clarification requests (never guesses).

    unsupported_dimension / unsupported_value: the ONLY structured channel
    for reporting that the user asked for a specific entity that does not
    exist in the catalogue (e.g. a part name, node, frequency, or metric).
    This produces NOT_FOUND — it must NOT be collapsed into a clarification.
    """

    model_config = ConfigDict(extra="forbid")

    metric: Optional[str] = None
    request: Optional[str] = None
    scope: Optional[str] = None
    frequency_selection: Optional[str] = None
    unit: Optional[str] = None
    params: Optional[ParamsDraft] = None
    #: which dimension holds a user-named value that is not in the catalogue
    unsupported_dimension: Optional[str] = None
    #: the user's verbatim value for that dimension (reported, not corrected)
    unsupported_value: Optional[str] = None
    #: set ONLY when the request type itself is outside catalogue/query
    #: capabilities (e.g. plotting, export, cross-catalogue analytics)
    unresolvable_reason: Optional[str] = None

    def missing_dimensions(self) -> list[str]:
        return [
            name
            for name in ("metric", "request", "scope", "frequency_selection", "unit")
            if getattr(self, name) is None
        ]


# ---------------------------------------------------------------------------
# Parser outcome
# ---------------------------------------------------------------------------


class ParseStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    PARSE_ERROR = "PARSE_ERROR"


class ParseOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: ParseStatus
    intent: Optional[QueryIntent] = None
    draft: Optional[QueryIntentDraft] = None
    message: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    candidates: list[QueryIntent] = Field(default_factory=list)
    candidate_hints: list[str] = Field(default_factory=list)
    #: dimensions filled deterministically from unique catalogue facts
    #: (explainable, never invented) — e.g. {"unit": "dBm"}
    inferred: dict = Field(default_factory=dict)
    #: dimension resolutions merged from a pending clarification (context)
    resolved_from_pending: dict = Field(default_factory=dict)
