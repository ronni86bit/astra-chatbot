"""JSON Schema for LLM structured output, constrained by the catalogue.

The schema embeds the CONTROLLED VOCABULARY as enums, so the model can only
select canonical catalogue values — it cannot invent dimensions. Parameter
values are plain numbers/enum qualifiers; request/parameter compatibility is
enforced afterwards by the existing QueryIntent Pydantic validation.
"""

from __future__ import annotations

from rf_catalogue.nlp.vocabulary import ControlledVocabulary

_DRAFT_PROPS_COMMON = {
    # NOTE: no JSON nulls and no "type": ["...", "null"] arrays. Some
    # provider validators (Groq) reject null values even when the schema
    # lists null in the type array. The convention across ALL providers is:
    # unknown/undetermined string fields are the EMPTY STRING "", and
    # "no parameters" is {"kind": "none"}. The parser normalizes "" -> None
    # before validation, so the rest of the pipeline still works with None.
    "metric": {"type": "string",
               "description": "Canonical metric code from the enum; empty string "
                              "if not determinable"},
    "request": {"type": "string",
                "description": "Canonical request type from the enum; empty string "
                               "if not determinable"},
    "scope": {"type": "string",
              "description": "Canonical scope value from the enum; empty string "
                             "if not determinable"},
    "frequency_selection": {"type": "string",
                            "description": "Canonical frequency selection from the "
                                           "enum; empty string if not determinable"},
    "unit": {"type": "string",
             "description": "Canonical unit from the enum; empty string if not "
                            "determinable"},
}


def build_draft_schema(vocab: ControlledVocabulary) -> dict:
    """Build the strict draft JSON schema for the given catalogue vocabulary."""
    props = {name: dict(spec) for name, spec in _DRAFT_PROPS_COMMON.items()}
    for name, values in (
        ("metric", vocab.metrics),
        ("request", vocab.requests),
        ("scope", vocab.scopes),
        ("frequency_selection", vocab.frequency_selections),
        ("unit", vocab.units),
    ):
        props[name]["enum"] = sorted(values) + [""]

    params_schema = {
        # NOTE: `anyOf`, not `oneOf` — OpenAI strict structured outputs
        # reject `oneOf` with HTTP 400 ("'oneOf' is not permitted").
        # NOTE (Groq): every anyOf variant must declare `properties` +
        # `additionalProperties:false`, and JSON-null variants are not
        # accepted — "no parameters" is expressed via {"kind": "none"}.
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Numeric threshold bound (may be negative)",
                    }
                },
                "required": ["threshold"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "closest_target": {
                        "type": "number",
                        "description": "Numeric closest-match target (may be negative)",
                    }
                },
                "required": ["closest_target"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "range_min": {"type": "number"},
                    "range_max": {"type": "number",
                                  "description": "must be strictly greater than range_min"},
                },
                "required": ["range_min", "range_max"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reference_qualifier": {
                        "type": "string",
                        "enum": sorted(vocab.reference_qualifiers),
                        "description": "Which reference quantity a power difference "
                                       "is taken against (verbatim catalogue phrases)",
                    }
                },
                "required": ["reference_qualifier"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["none"],
                             "description": "Set to 'none' when the request takes no "
                                            "parameters or none are determinable"},
                },
                "required": ["kind"],
            },
        ]
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **props,
            "params": {
                "anyOf": params_schema["anyOf"],
                "description": "Request-specific structured parameters. Use "
                               '{"kind": "none"} when none are needed or none '
                               "are determinable.",
            },
            "unsupported_dimension": {
                "type": "string",
                "enum": ["metric", "request", "scope", "frequency_selection",
                         "unit", "none"],
                "description": "Set when the user explicitly names a specific value "
                               "for this dimension that does NOT exist in the enum "
                               "(e.g. a part name, node number, frequency, or metric). "
                               "Use NOT for merely vague/ambiguous wording. "
                               "Use 'none' when everything mentioned exists or is "
                               "unknown.",
            },
            "unsupported_value": {
                "type": "string",
                "description": "The user's verbatim value for unsupported_dimension "
                               "(reported as-is, never corrected or invented). Empty "
                               "string when unsupported_dimension is 'none'.",
            },
            "unresolvable_reason": {
                "type": "string",
                "description": "Set ONLY when the request TYPE is outside the "
                               "catalogue's query capabilities (plotting, exporting, "
                               "comparing designs, predictions, small talk) — briefly "
                               "say why. Empty string for ordinary catalogue "
                               "questions, even when values are unknown or missing.",
            },
        },
        "required": [
            "metric", "request", "scope", "frequency_selection", "unit",
            "params", "unsupported_dimension", "unsupported_value",
            "unresolvable_reason",
        ],
    }
