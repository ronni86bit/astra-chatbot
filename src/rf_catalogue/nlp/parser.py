"""QueryParser: user text -> validated QueryIntent (never guesses).

Two-stage resolution:

Stage A (deterministic, small):
    whitespace/case/GHz->MHz/alias normalisation; authoritative longest-match
    metric alias identification; deterministic suggestion of catalogue values
    that literally occur in the text; re-use of the audited question grammar
    as a fallback for missing parameters; known unsupported-query detection.

Stage B (LLM):
    one structured-output call constrained by the catalogue-vocabulary JSON
    schema. The response is validated with Pydantic; invalid output FAILS.

Post-LLM deterministic policies (all explainable, catalogue-derived):
    metric override   exact unambiguous display-name alias overrides the model
    guess veto        scope/unit/frequency/request values the model invented
                      (not literally in the text) are vetoed unless the
                      catalogue uniquely confirms them
    catalogue         missing scope/unit/frequency/request are filled ONLY
    inference         when the catalogue uniquely determines them

Outcome states (never guesses):
    RESOLVED             validated QueryIntent ready for catalogue lookup
    NEEDS_CLARIFICATION  ambiguous/missing information (+ candidates)
    NOT_FOUND            specific request for something that does not exist
    UNSUPPORTED          request type outside catalogue capabilities
    PARSE_ERROR          LLM output failed structured validation
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import LLMClient, StructuredOutputError
from rf_catalogue.nlp.llm_schema import build_draft_schema
from rf_catalogue.nlp.normalizer import Normalizer
from rf_catalogue.nlp.schemas import (
    ClosestTargetParamsDraft,
    NoParamsDraft,
    ParseOutcome,
    ParseStatus,
    QueryIntentDraft,
    RangeParamsDraft,
    ReferenceQualifierDraft,
    ReferenceQualifierParamsDraft,
    ThresholdParamsDraft,
)
from rf_catalogue.nlp.vocabulary import (
    AMBIGUOUS_REQUEST_ALIASES,
    ControlledVocabulary,
    REQUEST_ALIASES,
    UNIT_ALIASES,
)
from rf_catalogue.query_intent import (
    REQUEST_PARAM_TYPES,
    ClosestTargetParams,
    NoParams,
    QueryIntent,
    RangeParams,
    ReferenceQualifier,
    ReferenceQualifierParams,
    ThresholdParams,
    parse_question_params,
)

_FREQ_RE = re.compile(r"\b(\d{3,4})\s*MHz\b", re.IGNORECASE)
_PART_RE = re.compile(r"\bpart\s+([A-Za-z0-9_]+)\b", re.IGNORECASE)
_NODE_NUM_RE = re.compile(r"\bnode\s+(\d+)\b", re.IGNORECASE)
_PART_STOPWORDS = {"and", "the", "a", "an", "of", "for", "to", "is", "in",
                   "on", "with", "at", "nodes", "every", "all"}
#: hedge words that turn an exact scope token into an ambiguous reference
_SCOPE_HEDGES = {"family", "group", "variants", "series"}

#: Explicit elliptical follow-up openings. ONLY these (or a very short
#: fragment with no self-contained query content) may inherit dimensions
#: from the previous turn. A grammatically complete question is always a
#: NEW intent: omitted dimensions are never silently inherited, because
#: context must never turn an exact catalogue question into NOT_FOUND.
_ELLIPTICAL_RE = re.compile(
    r"^(?:what|how)\s+about\b"
    r"|^and\s+(?:for|at|in|on|the|what|show|its|it's|that|across)\b"
    r"|^show\s+the\s+same\b"
    r"|^same\s+(?:for|as)\b"
    r"|^for\s+that\b"
    r"|^also\b"
    r"|^again\b"
    r"|^then\b",
    re.IGNORECASE,
)

_BASE_DIMS = ("metric", "request", "scope", "frequency_selection", "unit")

#: Audited power-difference phrasings (verbatim workbook grammar) ->
#: (metric, reference qualifier). Longest/most-specific first. These are the
#: catalogue's own question formulations, so matching them deterministically
#: is Stage-A grammar, not semantic guessing.
_POWER_PHRASES: tuple[tuple[str, str, ReferenceQualifier], ...] = (
    ("total node power above desired channel power", "TNP",
     ReferenceQualifier.DESIRED_CHANNEL_POWER),
    ("total node power above channel power", "TNP",
     ReferenceQualifier.CHANNEL_POWER),
    ("noise plus distortion power above channel noise power", "NDCP",
     ReferenceQualifier.CHANNEL_NOISE_POWER),
    ("channel power minus desired channel power", "CP",
     ReferenceQualifier.DESIRED_CHANNEL_POWER),
    ("output-to-input p1db difference", "OP1DB",
     ReferenceQualifier.OUTPUT_TO_INPUT),
    ("output-to-input saturation difference", "IPSAT",
     ReferenceQualifier.OUTPUT_TO_INPUT),
)

#: hypernym words that map to multiple catalogue metrics: the LLM choosing
#: one of them without an exact alias in the text is a guess -> clarify.
_METRIC_HYPERNYMS: dict[str, tuple[str, ...]] = {
    "gain": ("CGAIN", "GAIN"),
}

_SYSTEM_PROMPT = """You convert an RF measurement question into a structured JSON query \
for a fixed question-answer catalogue. You NEVER answer the question and you NEVER \
invent catalogue values.

Rules:
1. Every dimension you output MUST be copied verbatim from the JSON schema enums. \
If the user text does not determine a dimension, output null for it. Do not guess.
2. metric: e.g. "CGAIN" means Cascaded Gain, "GAIN" means Stage Power Gain \
(a different catalogue metric); "CP" is Channel Power, "CNP" is Channel Noise \
Power, "DCP" is Desired Channel Power - three different metrics. If the exact \
metric is unclear, output null.
3. REQUEST and FREQUENCY_SELECTION are DIFFERENT dimensions. REQUEST is WHAT \
operation the user wants; FREQUENCY_SELECTION is AT WHAT frequency. Frequency \
wording ("at 2500 MHz", "across frequency") must NEVER be placed in the request \
field. The request value "All frequencies" is a specific catalogue operation and \
is only correct when the user explicitly asks for that named operation.
4. "Best", "Worst", "Maximum" and "Minimum" are DISTINCT catalogue requests. \
Never merge them: "worst" -> "Worst", "minimum" -> "Minimum". Vague variants \
like "lowest", "smallest", "highest", "largest" are NOT mapped - output null \
for request so the system can ask the user. "Where is X above/below Y" maps to \
"Threshold Above"/"Threshold Below". "closest to Y" maps to "Closest value". \
"between A and B" maps to "Range". "for part X" (+ frequency-span wording) \
maps to "Part summary"; "for node X" maps to "Node summary".
5. scope: the exact catalogue scope (e.g. "Final node", "All nodes", part names \
like "ADL8124", node numbers like "2"). If the user does not name a scope, \
output null - NEVER guess one.
6. frequency_selection: exact catalogue values like "2500 MHz" or \
"All Frequencies". Frequencies are always written as MHz.
7. unit: only if the user states it or the metric makes it unambiguous; \
otherwise null. NEVER guess between dB and dBm.
8. params: only for these requests:
   - "Threshold Above"/"Threshold Below" -> {"threshold": number} (the numeric bound)
   - "Closest value" -> {"closest_target": number}
   - "Range" -> {"range_min": number, "range_max": number}
   - "Power difference"/"Individual frequency power difference" -> \
{"reference_qualifier": one of the enum phrases}
   - everything else -> {"kind": "none"} or null
   If the question needs a parameter value the user did not give, output null \
for params.
9. unsupported_dimension / unsupported_value: when the user EXPLICITLY names a \
specific entity that is not in the enum - a part ("part FOO123"), a node \
("node 99"), a frequency ("3400 MHz"), or a metric ("return loss") - set \
unsupported_dimension to that dimension and unsupported_value to the user's \
verbatim wording. This reports NOT_FOUND. Do NOT use this for vague or \
ambiguous wording; leave the dimension null instead.
10. unresolvable_reason: null for all ordinary catalogue questions. Set it ONLY \
when the request TYPE is outside the catalogue's query capabilities entirely \
(plotting graphs, exporting data, comparing two designs, predictions, small \
talk), then briefly say why.
When several catalogue values are equally plausible, output null for that \
dimension so the system can ask the user for clarification.
Output only the JSON object."""


class QueryParser:
    """Two-stage parser producing validated QueryIntent objects."""

    def __init__(
        self,
        engine: CatalogueEngine,
        llm_client: LLMClient,
        vocabulary: ControlledVocabulary | None = None,
    ):
        self.engine = engine
        self.llm = llm_client
        self.vocab = vocabulary or ControlledVocabulary.from_engine(engine)
        self.normalizer = Normalizer(self.vocab)
        self.schema = build_draft_schema(self.vocab)
        # metric -> unit is a verified 1:1 catalogue fact (audit-derived);
        # used for the conservative unit policy.
        self._metric_unit: dict[str, str] = {}
        for row in engine._catalogue:
            self._metric_unit.setdefault(row.metric, row.unit)

    # ------------------------------------------------------------------ API

    def parse(
        self, user_text: str, context: ConversationContext | None = None
    ) -> ParseOutcome:
        normalized = self.normalizer.normalize(user_text)

        # -- Stage A: pending clarification merge (before anything else) ----
        if context is not None and context.pending:
            merged = self._try_pending(user_text, normalized, context)
            if merged is not None:
                return merged

        # -- Stage A: deterministic unsupported-value detection -------------
        unsupported = self._detect_unsupported(normalized)
        if unsupported:
            return ParseOutcome(
                status=ParseStatus.NOT_FOUND,
                message=unsupported,
            )

        # -- Stage B: LLM structured interpretation -------------------------
        try:
            raw = self.llm.complete_structured(
                _SYSTEM_PROMPT, self._user_prompt(normalized, context), self.schema
            )
        except StructuredOutputError as exc:
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                message=f"The parser could not produce a valid structured query: {exc}",
            )

        try:
            draft = QueryIntentDraft.model_validate(raw)
        except ValidationError as exc:
            # Invalid structured output must fail validation — never guessed.
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                message="The parser returned structured data that failed schema "
                        f"validation: {exc.error_count()} error(s).",
            )

        # Cross-provider convention: unknown/undetermined string fields are
        # the EMPTY STRING (never JSON null); some models also emit the
        # literal strings "null"/"none". Normalize ALL of these to None
        # BEFORE the vocabulary check so unknown values flow into the
        # clarification path instead of tripping PARSE_ERROR.
        for dim in _BASE_DIMS:
            if getattr(draft, dim) == "":
                setattr(draft, dim, None)
        for field_name in ("unsupported_dimension", "unsupported_value",
                           "unresolvable_reason"):
            value = getattr(draft, field_name)
            if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
                setattr(draft, field_name, None)
        if draft.unsupported_dimension == "none":
            draft.unsupported_dimension = None

        # Defence-in-depth: providers without native schema support may emit
        # values outside the controlled vocabulary. Reject them explicitly.
        vocab_violations = self._vocabulary_violations(draft)
        if vocab_violations:
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                draft=draft,
                message="The parser selected values outside the catalogue "
                        f"vocabulary: {', '.join(vocab_violations)}.",
            )

        # NOT_FOUND: the user explicitly named a specific entity that does
        # not exist in the catalogue (reported via the structured channel).
        if draft.unsupported_dimension and draft.unsupported_dimension != "none":
            # Cross-validation (precedence step 4): a reported-unsupported
            # METRIC that is actually a known display name ("Mismatch Loss")
            # is resolved, not rejected — the enum only carries codes.
            corrected = None
            if draft.unsupported_dimension == "metric":
                resolved_metric = self.vocab.resolve_metric(
                    draft.unsupported_value or "")
                if resolved_metric is not None:
                    draft.metric = resolved_metric
                    corrected = resolved_metric
            if corrected is None:
                return ParseOutcome(
                    status=ParseStatus.NOT_FOUND,
                    draft=draft,
                    message=(
                        f"The requested {draft.unsupported_dimension.replace('_', ' ')} "
                        f"{draft.unsupported_value!r} does not exist in the catalogue."
                    ),
                )
            draft.unsupported_dimension = None
            draft.unsupported_value = None
            inferred["metric (unsupported-claim corrected)"] = corrected

        # UNSUPPORTED: the request type itself is outside catalogue/query
        # capabilities (plotting, export, analytics, ...).
        if draft.unresolvable_reason:
            return ParseOutcome(
                status=ParseStatus.UNSUPPORTED,
                draft=draft,
                message=(
                    "This request type is not supported by the catalogue: "
                    f"{draft.unresolvable_reason}"
                ),
            )

        inferred: dict[str, str] = {}

        # -- Audited power-difference phrase grammar (deterministic) --------
        phrase_hit = self._apply_power_phrase_grammar(draft, normalized)
        if phrase_hit:
            inferred["power-difference phrase"] = phrase_hit

        # -- Metric override: exact unambiguous alias is authoritative ------
        # (skipped when the audited phrase grammar already fixed the metric:
        # e.g. "total node power above desired channel power" contains the
        # "desired channel power" alias but means TNP, not DCP.)
        if not phrase_hit:
            override = self._apply_metric_override(draft, normalized)
            if override:
                inferred["metric (alias)"] = override

        # -- Bare-hypernym ambiguity: bare "gain" -> clarify, never guess ---
        hypernym = self._metric_hypernym_ambiguity(draft, normalized)
        if hypernym:
            return self._clarification_with_candidates(
                draft,
                missing=["metric"],
                hints=["I found two possible metrics: "
                       + ", ".join(f"{i + 1}. {m}"
                                   for i, m in enumerate(hypernym))
                       + ". Which one do you mean?"],
                dim="metric",
                options=list(hypernym),
                inferred=inferred,
                context=context,
            )

        # -- Guess veto: non-literal scope/unit/frequency/request values ----
        vetoed = self._apply_guess_vetoes(draft, normalized)
        if vetoed:
            inferred["vetoed (not stated by user)"] = ", ".join(vetoed)

        # -- Stage A follow-up: deterministic fills for null dimensions -----
        self._fill_from_text(draft, normalized)

        # -- Conversation context inheritance (conservative) -----------------
        # ONLY for explicit elliptical follow-ups ("What about the best?",
        # "And at 2600?", "Show the same for node 45."). A new, grammatically
        # complete question is a NEW intent by default: omitted dimensions
        # are left to the catalogue-inference/clarification policy, never
        # silently inherited from prior turns. For elliptical follow-ups the
        # stable dimensions — including the previous operation (request) —
        # are inherited when the new message does not restate them; params
        # are never inherited.
        if context is not None and self.is_elliptical_followup(normalized):
            inherited = context.inherit(set(draft.missing_dimensions()))
            for dim, value in inherited.items():
                setattr(draft, dim, value)
                inferred[f"{dim} (context)"] = value

        # -- Catalogue inference: unique determination only ------------------
        inferred.update(self._catalogue_infer(draft))

        # Literal-but-conflicting unit: the user stated a unit that no
        # catalogue row uses for this metric. Complete the remaining
        # dimensions using the catalogue-consistent unit, keep the user's
        # stated unit, and let the lookup report NOT_FOUND.
        if draft.missing_dimensions() and draft.unit and \
                self._metric_unit.get(draft.metric) not in (None, draft.unit):
            canonical_unit = self._metric_unit[draft.metric]
            saved = draft.unit
            draft.unit = canonical_unit
            inferred.update(self._catalogue_infer(draft))
            draft.unit = saved
            if not draft.missing_dimensions():
                inferred["unit (conflicts with catalogue)"] = saved

        # -- Scope ambiguity (genuinely ambiguous scope) -> clarification ---
        # Fires when scope is unresolved OR when hedge wording ("family")
        # makes an otherwise-literal scope ambiguous vs its catalogue
        # extension variants (ADL8124 vs ADL8124_1).
        hedged = any(re.search(rf"\b{h}\b", normalized, re.IGNORECASE)
                     for h in _SCOPE_HEDGES)
        scope_candidates = self._scope_candidates(normalized)
        scope_uncertain = draft.scope is None or (
            hedged and len(scope_candidates) > 1)
        if scope_uncertain and len(scope_candidates) > 1:
            draft.scope = None
            return self._clarification_with_candidates(
                draft,
                missing=["scope"],
                hints=["Which scope do you mean? Candidates: "
                       + ", ".join(scope_candidates)],
                dim="scope",
                options=scope_candidates,
                inferred=inferred,
                context=context,
            )

        # -- Request variant ambiguity ("lowest" etc.) -> clarification -----
        # Fires even when the model already chose one of the variant's
        # candidates: catalogue REQUEST values are never silently merged.
        ambiguous_request = self._ambiguous_request_variant(normalized)
        if ambiguous_request is not None:
            variants = AMBIGUOUS_REQUEST_ALIASES[ambiguous_request]
            if draft.request is None or draft.request in variants:
                return self._clarification_with_candidates(
                    draft,
                    missing=["request"],
                    hints=[
                        f"The wording {ambiguous_request!r} could mean several "
                        f"distinct catalogue requests: {', '.join(variants)}. "
                        f"Which one do you want?"
                    ],
                    dim="request",
                    options=list(variants),
                    inferred=inferred,
                    context=context,
                )

        # -- Clarification for still-missing dimensions ----------------------
        missing = draft.missing_dimensions()
        if missing:
            options = getattr(self, "_last_infer_options", {})
            if len(missing) == 1 and missing[0] in options:
                opts = options[missing[0]]
                return self._clarification_with_candidates(
                    draft,
                    missing=missing,
                    hints=[f"Which {missing[0]} do you mean? Candidates: "
                           + ", ".join(opts)],
                    dim=missing[0],
                    options=opts,
                    inferred=inferred,
                    context=context,
                )
            outcome = self._clarification(draft, missing)
            outcome.inferred = inferred
            if context is not None:
                context.set_pending(self._draft_dict(draft), missing)
            return outcome

        # -- Parameter resolution --------------------------------------------
        params, missing_params, params_error = self._resolve_params(draft, normalized)
        if params_error:
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                draft=draft,
                message=params_error,
            )
        if missing_params:
            outcome = self._clarification(draft, missing_params)
            outcome.inferred = inferred
            if context is not None:
                context.set_pending(self._draft_dict(draft), missing_params)
            return outcome

        # -- Final validation through the trusted QueryIntent model ----------
        try:
            intent = QueryIntent(
                metric=draft.metric,
                request=draft.request,
                scope=draft.scope,
                frequency_selection=draft.frequency_selection,
                unit=draft.unit,
                params=params,
            )
        except ValidationError as exc:
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                draft=draft,
                message="The parsed query failed catalogue validation: "
                        f"{exc.error_count()} error(s).",
            )

        if context is not None:
            context.clear_pending()
        return ParseOutcome(status=ParseStatus.RESOLVED, intent=intent,
                            draft=draft, inferred=inferred)

    # ------------------------------------------------------------- internals

    @staticmethod
    def _draft_dict(draft: QueryIntentDraft) -> dict:
        return {dim: getattr(draft, dim) for dim in _BASE_DIMS}

    # -- pending clarification ------------------------------------------------

    def _try_pending(
        self, user_text: str, normalized: str, context: ConversationContext
    ) -> ParseOutcome | None:
        """Complete a pending clarification from a short user reply.

        Conservative gate: the reply must be short (<= 4 tokens) and must
        deterministically resolve at least one pending dimension. Anything
        longer is treated as a fresh question.
        """
        pending = context.pending
        if not pending:
            return None
        tokens = normalized.split()
        if len(tokens) > 4:
            context.clear_pending()
            return None

        draft_values = dict(pending["draft"])
        missing = list(pending["missing"])
        resolved: dict[str, str] = {}
        text_lower = normalized.lower()

        for dim in list(missing):
            value = None
            if dim == "metric":
                m = self.vocab.match_metric_alias(normalized)
                if m:
                    value = m[0]
                else:
                    token = normalized.strip().upper()
                    if token in self.vocab.metrics:
                        value = token
            elif dim == "request":
                value = self.vocab.resolve_request(normalized)
            elif dim == "scope":
                candidates = self._scope_candidates(normalized)
                if len(candidates) == 1:
                    value = candidates[0]
            elif dim == "unit":
                value = self.vocab.resolve_unit(normalized)
                if value is None and normalized.strip() in self.vocab.units:
                    value = normalized.strip()
            elif dim == "frequency_selection":
                m = _FREQ_RE.search(normalized)
                if m:
                    candidate = f"{int(m.group(1))} MHz"
                    if candidate in self.vocab.frequency_selections:
                        value = candidate
                elif re.search(r"\ball frequencies\b", text_lower):
                    value = "All Frequencies"
            if value is not None:
                resolved[dim] = value
                draft_values[dim] = value
                missing.remove(dim)

        if not resolved:
            # the reply did not resolve anything pending; treat as fresh
            context.clear_pending()
            return None

        if missing:
            # partially resolved: run catalogue inference on the merged draft
            # (e.g. metric now known -> unit uniquely determined), keep pending
            merged_draft = QueryIntentDraft(**{**draft_values, "params": None})
            inferred = self._catalogue_infer(merged_draft)
            for dim, value in inferred.items():
                draft_values[dim] = value
                if dim in missing:
                    missing.remove(dim)
            if missing:
                context.set_pending(draft_values, missing)
                return ParseOutcome(
                    status=ParseStatus.NEEDS_CLARIFICATION,
                    missing_fields=missing,
                    candidate_hints=[f"Still need: {', '.join(missing)}."],
                    resolved_from_pending=resolved,
                    inferred=inferred,
                    message="Thanks - noted. I still need a little more "
                            "information.",
                )

        # fully resolved -> build the intent without the LLM
        context.clear_pending()
        # Catalogue inference on the merged draft (same policy as the
        # partially-resolved branch): a dimension the user never stated
        # may be filled ONLY when the catalogue uniquely determines it.
        merged_draft = QueryIntentDraft(**{**draft_values, "params": None})
        inferred = self._catalogue_infer(merged_draft)
        for dim, value in inferred.items():
            draft_values[dim] = value
        still_missing = [d for d in _BASE_DIMS if draft_values.get(d) is None]
        if still_missing:
            context.set_pending(draft_values, still_missing)
            return ParseOutcome(
                status=ParseStatus.NEEDS_CLARIFICATION,
                missing_fields=still_missing,
                candidate_hints=[f"Still need: {', '.join(still_missing)}."],
                resolved_from_pending=resolved,
                inferred=inferred,
                message="Thanks - noted. I still need a little more "
                        "information.",
            )
        params, missing_params, params_error = self._params_for_request(
            draft_values["request"], normalized, draft_values.get("unit"))
        if params_error or missing_params:
            return ParseOutcome(
                status=ParseStatus.NEEDS_CLARIFICATION,
                missing_fields=missing_params,
                resolved_from_pending=resolved,
                message=params_error
                        or "Thanks - noted. I still need the request-specific "
                           "parameter value.",
            )
        try:
            intent = QueryIntent(
                metric=draft_values["metric"],
                request=draft_values["request"],
                scope=draft_values["scope"],
                frequency_selection=draft_values["frequency_selection"],
                unit=draft_values["unit"],
                params=params,
            )
        except ValidationError as exc:
            return ParseOutcome(
                status=ParseStatus.PARSE_ERROR,
                message=f"The completed query failed catalogue validation: "
                        f"{exc.error_count()} error(s).",
            )
        return ParseOutcome(
            status=ParseStatus.RESOLVED,
            intent=intent,
            inferred=inferred,
            resolved_from_pending=resolved,
            message=f"Completed from your clarification: {resolved}",
        )

    # -- metric override -------------------------------------------------------

    def is_elliptical_followup(self, normalized: str) -> bool:
        """True when the message is an explicit elliptical follow-up that
        may inherit dimensions from the previous turn.

        Two conservative signals:
        1. the message OPENS with an elliptical marker ("what about",
           "and for that", "show the same", ...); or
        2. the message is a very short fragment (<= 4 tokens) with no
           self-contained query content (no metric alias, no scope token,
           no frequency), e.g. "the best" / "for node 45".

        Everything else — including any full question that merely omits a
        dimension — is treated as a new intent and never inherits.
        """
        if _ELLIPTICAL_RE.match(normalized.strip()):
            return True
        tokens = normalized.split()
        if 0 < len(tokens) <= 4:
            # A short fragment without its own metric is elliptical ("the
            # best", "for node 45"); a metric alias makes it self-contained.
            if self.vocab.match_metric_alias(normalized) is None \
                    and not _FREQ_RE.search(normalized) \
                    and not re.search(r"\ball frequencies\b", normalized,
                                      re.IGNORECASE):
                return True
        return False

    def _apply_power_phrase_grammar(
        self, draft: QueryIntentDraft, normalized: str
    ) -> str | None:
        """Authoritative Stage-A grammar for audited power-difference phrases.

        The workbook formulates these questions with fixed wording; when the
        full phrase is present, metric/request/qualifier are set (or
        corrected) deterministically.
        """
        text_lower = normalized.lower()
        for phrase, metric, qualifier in _POWER_PHRASES:
            if phrase in text_lower:
                draft.metric = metric
                if "individual frequency" in text_lower:
                    draft.request = "Individual frequency power difference"
                else:
                    draft.request = "Power difference"
                draft.params = ReferenceQualifierParamsDraft(
                    reference_qualifier=ReferenceQualifierDraft(qualifier.value)
                )
                return phrase
        return None

    def _metric_hypernym_ambiguity(
        self, draft: QueryIntentDraft, normalized: str
    ) -> tuple[str, ...] | None:
        """Bare hypernym wording ('gain') maps to multiple metrics.

        When the text contains a hypernym, no exact alias/canonical token
        disambiguates it, and the model selected one of the plausible
        metrics, the choice is a guess -> clarify with all candidates.
        """
        text_lower = normalized.lower()
        for hypernym, candidates in _METRIC_HYPERNYMS.items():
            if re.search(rf"\b{re.escape(hypernym)}\b", text_lower) \
                    and draft.metric in candidates \
                    and self.vocab.match_metric_alias(normalized) is None \
                    and not any(w == w.upper() and w.upper() in self.vocab.metrics
                                for w in normalized.split()):
                return candidates
        return None

    def _apply_metric_override(self, draft: QueryIntentDraft, normalized: str) -> str | None:
        """Exact unambiguous display-name alias overrides the model.

        Precedence (mission policy): exact canonical token > exact
        unambiguous alias > LLM. Substring matching is never used; only the
        longest matched alias wins, and only when it maps to exactly one
        metric.
        """
        canonical = None
        for word in normalized.split():          # precedence 1: canonical token
            # ALL-CAPS only: the user explicitly typing a code ("CGAIN").
            # A lowercase natural word like "gain" must NOT uppercase-match
            # the GAIN metric code (it stays ambiguous -> clarification).
            if word == word.upper() and word.upper() in self.vocab.metrics \
                    and len(word) > 2:
                canonical = word.upper()
                break
        if canonical is None:
            m = self.vocab.match_metric_alias(normalized)
            canonical = m[0] if m else None      # precedence 2: unique alias
        if canonical is None or canonical == draft.metric:
            return None
        draft.metric = canonical                 # override (or fill) authoritatively
        return canonical

    # -- guess vetoes ------------------------------------------------------------

    def _apply_guess_vetoes(self, draft: QueryIntentDraft, normalized: str) -> list[str]:
        """Veto values the model invented (not literally stated by the user).

        A vetoed value is kept ONLY if the catalogue uniquely confirms it
        (e.g. unit is uniquely determined by the metric). Everything else is
        nulled so the normal missing-dimension policy applies.
        """
        vetoed: list[str] = []
        text_lower = normalized.lower()

        # scope: literal catalogue token in the text? Numeric scopes (node
        # numbers) are ONLY literal via the explicit "node N" pattern — a
        # bare number in the text is usually a threshold/closest value.
        if draft.scope is not None:
            if draft.scope.isdigit():
                literal = bool(_NODE_NUM_RE.search(normalized)) and any(
                    m.group(1) == draft.scope
                    for m in _NODE_NUM_RE.finditer(normalized))
            else:
                literal = bool(re.search(rf"\b{re.escape(draft.scope)}\b",
                                         normalized, re.IGNORECASE))
            if not literal:
                draft.scope = None
                vetoed.append("scope")

        # unit: literal in the text? (case-insensitive: "dbm" == "dBm")
        if draft.unit is not None:
            literal = any(re.search(rf"\b{re.escape(u)}\b", text_lower,
                                    re.IGNORECASE)
                          for u in self.vocab.units)
            if not literal:
                expected = self._metric_unit.get(draft.metric or "")
                if expected == draft.unit:
                    pass  # catalogue uniquely confirms the metric's unit
                else:
                    draft.unit = None
                    vetoed.append("unit")

        # frequency: literal MHz value or explicit all-frequencies wording?
        if draft.frequency_selection is not None:
            literal = bool(_FREQ_RE.search(normalized))
            if not literal and draft.frequency_selection == "All Frequencies":
                literal = bool(re.search(
                    r"\ball frequencies\b|\bacross frequency\b"
                    r"|\bacross all frequencies\b", text_lower))
            if not literal and \
                    draft.frequency_selection in self.vocab.frequency_selections:
                # Bare-number wording ("And at 2600?"): the number alone is
                # a literal frequency statement when it is itself a
                # catalogue frequency selection. Threshold/closest values
                # are decimals routed to params, so they never collide.
                number = draft.frequency_selection.split()[0]
                literal = bool(
                    re.search(rf"\b{re.escape(number)}\b", normalized))
            if not literal:
                draft.frequency_selection = None
                vetoed.append("frequency_selection")

        # request: "All frequencies" must never come from frequency wording
        if draft.request == "All frequencies" and \
                not re.search(r"\ball frequencies\b", text_lower):
            draft.request = None
            vetoed.append("request (frequency wording)")
        return vetoed

    # -- catalogue inference -------------------------------------------------

    def _catalogue_infer(
        self, draft: QueryIntentDraft
    ) -> dict[str, str]:
        """Fill missing base dimensions the catalogue uniquely determines.

        A dimension is inferred only when filtering the audited index by the
        currently known dimensions yields EXACTLY ONE distinct value for it.
        Inference requires the metric as an anchor. Everything is explainable
        and catalogue-derived; nothing is invented. Near-misses (2+ values)
        are recorded in ``self._last_infer_options`` so the clarification
        flow can present concrete candidates.
        """
        inferred: dict[str, str] = {}
        self._last_infer_options: dict[str, list[str]] = {}
        if not draft.metric:
            return inferred

        known = {dim: getattr(draft, dim) for dim in _BASE_DIMS}
        for _ in range(3):  # fixpoint: filling one dim can unlock another
            changed = False
            for dim in _BASE_DIMS:
                if known[dim] is not None:
                    continue
                others = {d: v for d, v in known.items() if d != dim and v is not None}
                if len(others) < 2:
                    continue
                values = {
                    key[_BASE_DIMS.index(dim)]
                    for key in self.engine._index
                    if all(key[_BASE_DIMS.index(d)] == v
                           for d, v in others.items())
                }
                if len(values) == 1:
                    value = values.pop()
                    setattr(draft, dim, value)
                    known[dim] = value
                    inferred[dim] = value
                    changed = True
                elif len(values) > 1:
                    self._last_infer_options[dim] = sorted(values)
            if not changed:
                break
        return inferred

    # -- vocabulary / prompt helpers ------------------------------------------

    def _vocabulary_violations(self, draft: QueryIntentDraft) -> list[str]:
        """Return dimension names whose draft values are not canonical."""
        checks = (
            ("metric", draft.metric, self.vocab.metrics),
            ("request", draft.request, self.vocab.requests),
            ("scope", draft.scope, self.vocab.scopes),
            ("frequency_selection", draft.frequency_selection,
             self.vocab.frequency_selections),
            ("unit", draft.unit, self.vocab.units),
        )
        return [
            f"{name}={value!r}"
            for name, value, allowed in checks
            if value is not None and value not in allowed
        ]

    @staticmethod
    def _user_prompt(normalized_text: str, context: ConversationContext | None) -> str:
        context_line = (
            context.summary_for_prompt() if context is not None else "none"
        )
        return (
            f"Previous answered query context (for follow-up questions "
            f"only — do not copy blindly): {context_line}\n"
            f"User question: {normalized_text}"
        )

    def _detect_unsupported(self, normalized: str) -> str | None:
        """Deterministic detection of explicit out-of-catalogue values."""
        for m in _FREQ_RE.finditer(normalized):
            freq = f"{int(m.group(1))} MHz"
            if freq not in self.vocab.frequency_selections:
                return (
                    f"Frequency {freq!r} is not in the catalogue "
                    f"(supported: 1200-3000 MHz in 100 MHz steps, "
                    f"or 'All Frequencies')."
                )
        m = _PART_RE.search(normalized)
        if m:
            token = m.group(1)
            if token.lower() not in _PART_STOPWORDS and not any(
                s.lower() == token.lower() for s in self.vocab.scopes
            ):
                return (f"Part {token!r} does not exist in the catalogue.")
        m = _NODE_NUM_RE.search(normalized)
        if m:
            node = m.group(1)
            if node not in self.vocab.scopes:
                return f"Node {node!r} does not exist in the catalogue."
        return None

    def _scope_candidates(self, normalized: str) -> list[str]:
        """Catalogue scopes plausibly meant by the text.

        Exact word-boundary matches win; catalogue scopes that merely extend
        a matched token (ADL8124 -> ADL8124_1) count as competing candidates
        only when hedge wording is present ("family", "group", ...), in which
        case both are returned for clarification.
        """
        exact: set[str] = set()
        for scope in self.vocab.scopes:
            if re.search(rf"\b{re.escape(scope)}\b", normalized, re.IGNORECASE):
                exact.add(scope)
        for m in _NODE_NUM_RE.finditer(normalized):
            if m.group(1) in self.vocab.scopes:
                exact.add(m.group(1))
        hedged = any(re.search(rf"\b{h}\b", normalized, re.IGNORECASE)
                     for h in _SCOPE_HEDGES)
        if exact and not hedged:
            return sorted(exact)  # exact scope is authoritative
        extended: set[str] = set(exact)
        for scope in exact:
            for other in self.vocab.scopes:
                if other != scope and other.upper().startswith(scope.upper() + "_"):
                    extended.add(other)
        return sorted(extended)

    def _ambiguous_request_variant(self, normalized: str) -> str | None:
        """Return the surface variant if the text uses an ambiguous extreme
        word ('lowest', 'highest', ...) and no canonical request was set."""
        text_lower = normalized.lower()
        for variant in AMBIGUOUS_REQUEST_ALIASES:
            if re.search(rf"\b{re.escape(variant)}\b", text_lower):
                return variant
        return None

    def _fill_from_text(self, draft: QueryIntentDraft, normalized: str) -> None:
        """Deterministically fill null dimensions the text states explicitly.

        Only unambiguous mechanical matches are used — never semantic guesses.
        """
        text_lower = normalized.lower()
        if draft.metric is None:
            candidates = self.vocab.candidates_for_metric(text_lower)
            if len(candidates) == 1:
                draft.metric = candidates[0]
        if draft.request is None:
            matches = {
                canonical
                for alias, canonical in REQUEST_ALIASES.items()
                if re.search(rf"\b{re.escape(alias)}\b", text_lower)
            }
            if len(matches) == 1:
                draft.request = matches.pop()
        if draft.scope is None:
            # Exact single catalogue scope occurring in the text (part names,
            # "Final node"/"All nodes", node numbers via "node N"). Ambiguous
            # cases (>1 candidate) are handled by the caller's clarification.
            scope_candidates = [
                c for c in self._scope_candidates(normalized)
                if not (c.isdigit() and not _NODE_NUM_RE.search(normalized))
            ]
            if len(scope_candidates) == 1:
                draft.scope = scope_candidates[0]
        if draft.frequency_selection is None:
            freqs = {f"{int(m.group(1))} MHz" for m in _FREQ_RE.finditer(normalized)}
            freqs = {f for f in freqs if f in self.vocab.frequency_selections}
            if len(freqs) == 1:
                draft.frequency_selection = freqs.pop()
            elif re.search(r"\bAll Frequencies\b", normalized) or \
                    re.search(r"\bacross (all )?frequenc(?:y|ies)\b", text_lower):
                draft.frequency_selection = "All Frequencies"
        if draft.unit is None:
            matches = {
                canonical
                for alias, canonical in UNIT_ALIASES.items()
                if re.search(rf"\b{re.escape(alias)}\b", text_lower)
            }
            matches &= set(self.vocab.units)
            if len(matches) == 1:
                draft.unit = matches.pop()

    def _clarification_with_candidates(
        self,
        draft: QueryIntentDraft,
        missing: list[str],
        hints: list[str],
        dim: str,
        options: list[str],
        inferred: dict[str, str] | None = None,
        context: ConversationContext | None = None,
    ) -> ParseOutcome:
        """Clarification carrying fully-built candidate intents when the
        remaining dimensions are already determined.

        The unfinished draft is also stored as the pending clarification so
        a short candidate reply ("CGAIN", "Final node", "Worst") resumes
        the query instead of re-parsing from scratch (README §9 policy).
        """
        if context is not None:
            context.set_pending(self._draft_dict(draft), missing)
        candidates: list[QueryIntent] = []
        if all(getattr(draft, d) is not None
               for d in _BASE_DIMS if d != dim):
            for option in options:
                values = {d: getattr(draft, d) for d in _BASE_DIMS}
                values[dim] = option
                try:
                    candidates.append(QueryIntent(**values, params=NoParams()))
                except ValidationError:
                    continue
        return ParseOutcome(
            status=ParseStatus.NEEDS_CLARIFICATION,
            draft=draft,
            missing_fields=missing,
            candidate_hints=hints,
            candidates=candidates,
            inferred=inferred or {},
            message="I need more information before I can look this up.",
        )

    def _resolve_params(
        self, draft: QueryIntentDraft, normalized: str
    ) -> tuple[object, list[str], str | None]:
        """Build concrete params for the draft's request.

        Returns (params, missing_fields, error_message). Fallback when the
        LLM left params null: re-use the audited question grammar on the
        user text itself (deterministic, verified invariants).
        """
        request = draft.request
        required = REQUEST_PARAM_TYPES.get(request, NoParams)

        if required is NoParams:
            if draft.params is None or isinstance(draft.params, NoParamsDraft):
                return NoParams(), [], None
            return None, [], (
                f"Request {request!r} takes no parameters, but the parser "
                f"produced {type(draft.params).__name__}."
            )

        # Draft supplied params: convert draft model -> concrete params,
        # cross-checking every numeric value against the user text. A number
        # that appears nowhere in the question was guessed by the model
        # (e.g. threshold 0 for "above the limit") and is vetoed.
        def _numeric_literal(value: float) -> bool:
            for form in (repr(value), f"{value:g}", str(int(value))
                         if float(value).is_integer() else None):
                if form and re.search(rf"(?<![\w.]){re.escape(form)}(?![\w.])",
                                      normalized):
                    return True
            return False

        p = draft.params
        if isinstance(p, ThresholdParamsDraft):
            if not _numeric_literal(p.threshold):
                return None, ["params.threshold"], None
            return self._typed(required, ThresholdParams(threshold=p.threshold))
        if isinstance(p, ClosestTargetParamsDraft):
            if not _numeric_literal(p.closest_target):
                return None, ["params.closest_target"], None
            return self._typed(required, ClosestTargetParams(closest_target=p.closest_target))
        if isinstance(p, RangeParamsDraft):
            if not (_numeric_literal(p.range_min) and _numeric_literal(p.range_max)):
                return None, ["params.range bounds (min and max)"], None
            return self._typed(required, RangeParams(range_min=p.range_min, range_max=p.range_max))
        if isinstance(p, ReferenceQualifierParamsDraft):
            return self._typed(required, ReferenceQualifierParams(
                reference_qualifier=ReferenceQualifier(p.reference_qualifier.value)))

        # params missing -> deterministic fallback via the audited grammar
        try:
            fallback = parse_question_params(request, normalized, draft.unit)
        except Exception:
            fallback = None
        if fallback is not None and isinstance(fallback, required):
            return fallback, [], None

        param_name = {
            ThresholdParams: "threshold",
            ClosestTargetParams: "closest_target",
            RangeParams: "range bounds (min and max)",
            ReferenceQualifierParams: "reference qualifier",
        }[required]
        return None, [f"params.{param_name}"], None

    def _params_for_request(
        self, request: str, normalized: str, unit: str | None
    ) -> tuple[object, list[str], str | None]:
        """Params for a request built from a pending-clarification merge."""
        required = REQUEST_PARAM_TYPES.get(request, NoParams)
        if required is NoParams:
            return NoParams(), [], None
        try:
            fallback = parse_question_params(request, normalized, unit)
        except Exception:
            fallback = None
        if fallback is not None and isinstance(fallback, required):
            return fallback, [], None
        return None, [f"params for {request}"], None

    @staticmethod
    def _typed(required_type, params_obj):
        """Ensure the produced params match the request's required type."""
        if isinstance(params_obj, required_type):
            return params_obj, [], None
        return None, [], (
            f"Request {required_type} requires {required_type.__name__}, "
            f"got {type(params_obj).__name__}."
        )

    def _clarification(
        self,
        draft: QueryIntentDraft,
        missing: list[str],
    ) -> ParseOutcome:
        hints: list[str] = []
        if "metric" in missing:
            hints.append(
                "Which metric do you mean? (e.g. CGAIN = Cascaded Gain, "
                "GAIN = Stage Power Gain, MismatchLoss, DCP, CP, TNP, ...)"
            )
        if "request" in missing:
            hints.append(
                "What kind of question is this? (Best, Worst, Minimum, Maximum, "
                "Threshold Above/Below, Closest value, Range, summaries, ...)"
            )
        if "scope" in missing:
            hints.append(
                "Which scope? (e.g. 'Final node', 'All nodes', a part such as "
                "'ADL8124', or a node number such as '2')"
            )
        if "frequency_selection" in missing:
            hints.append(
                "Which frequency? (e.g. '1500 MHz' or 'All Frequencies')"
            )
        if "unit" in missing:
            hints.append("Which unit? (dB, dBm, dB20, deg, Ohm)")
        for field_name in missing:
            if field_name.startswith("params."):
                hints.append(f"Missing required {field_name.split('.', 1)[1]}.")
        return ParseOutcome(
            status=ParseStatus.NEEDS_CLARIFICATION,
            draft=draft,
            missing_fields=missing,
            candidate_hints=hints,
            message="I need more information before I can look this up.",
        )
