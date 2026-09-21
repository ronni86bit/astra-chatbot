"""End-to-end service: natural-language question -> exact catalogue answer.

Flow (milestone 2 architecture):

    user text
      -> Stage A deterministic normalization
      -> Stage B LLM structured parsing (QueryIntentDraft)
      -> Pydantic validation (QueryIntent)
      -> deterministic catalogue lookup (lookup_by_intent)
      -> exact stored ANSWER (verbatim, traceable to row ID)

TRUST RULES:
- The answer text is ALWAYS the catalogue row's stored ANSWER, unaltered.
- The LLM never generates, calculates, or modifies answers.
- Ambiguous/missing information produces NEEDS_CLARIFICATION, never a guess.
- Every ANSWERED result carries the catalogue row ID for traceability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import LLMClient
from rf_catalogue.nlp.parser import QueryParser
from rf_catalogue.nlp.schemas import ParseOutcome, ParseStatus
from rf_catalogue.query_intent import QueryIntent


class AnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    PARSE_ERROR = "PARSE_ERROR"
    ERROR = "ERROR"


@dataclass
class AnswerResult:
    """Trusted result envelope.

    For ANSWERED results every catalogue field is the verbatim stored row
    content and ``row_id`` provides exact traceability. The LLM never
    touches ``answer_text``.
    """

    status: AnswerStatus
    message: str = ""
    answer_text: str | None = None      # verbatim catalogue ANSWER
    question_text: str | None = None    # verbatim catalogue QUESTION
    row_id: int | None = None           # traceability
    metric: str | None = None
    request: str | None = None
    scope: str | None = None
    unit: str | None = None
    frequency_selection: str | None = None
    answer_type: str | None = None
    intent: QueryIntent | None = None
    parse: ParseOutcome | None = None
    missing_fields: list[str] = field(default_factory=list)
    candidate_hints: list[str] = field(default_factory=list)
    candidates: list = field(default_factory=list)


def _provider_error_message(exc: Exception) -> str:
    """Map provider exceptions to safe, useful user-facing guidance.

    Never includes secrets; includes only the exception class name and
    coarse error classification (status codes / keywords).
    """
    detail = f"{exc}"
    lowered = detail.lower()
    if "401" in lowered or "unauthorized" in lowered or "authentication" in lowered:
        return ("The LLM provider rejected the API key (authentication "
                "failure). Check OPENROUTER_API_KEY in your .env or "
                "environment and try again.")
    if "402" in lowered or "credit" in lowered:
        return ("The OpenRouter account has insufficient credits for this "
                "request. Top up credits at openrouter.ai/settings/credits; "
                "offline catalogue features remain available.")
    if "429" in lowered or "rate" in lowered and "limit" in lowered:
        return ("The LLM provider is rate-limiting requests. Wait a moment "
                "and try again.")
    if "timeout" in lowered or "timed out" in lowered:
        return ("The LLM provider did not respond in time. Please try again.")
    return (f"Sorry - the request could not be completed "
            f"({type(exc).__name__}). Please try again.")


class RfCatalogueService:
    """Public service facade for the deterministic catalogue chatbot."""

    def __init__(
        self,
        engine: CatalogueEngine | None = None,
        llm_client: LLMClient | None = None,
        env_file: str | None = None,
    ):
        self.engine = engine or CatalogueEngine()
        if llm_client is None:
            from rf_catalogue.nlp.llm_client import default_client

            llm_client = default_client(env_file=env_file)
        self.parser = QueryParser(self.engine, llm_client)

    def answer_question(
        self,
        user_text: str,
        conversation_context: ConversationContext | None = None,
    ) -> AnswerResult:
        try:
            outcome = self.parser.parse(user_text, conversation_context)
        except Exception as exc:  # graceful degradation, no tracebacks to users
            return AnswerResult(
                status=AnswerStatus.ERROR,
                message=_provider_error_message(exc),
            )

        if outcome.status is ParseStatus.NEEDS_CLARIFICATION:
            return AnswerResult(
                status=AnswerStatus.NEEDS_CLARIFICATION,
                message=outcome.message,
                parse=outcome,
                missing_fields=outcome.missing_fields,
                candidate_hints=outcome.candidate_hints,
                candidates=outcome.candidates,
                intent=outcome.intent,
            )

        if outcome.status is ParseStatus.NOT_FOUND:
            return AnswerResult(
                status=AnswerStatus.NOT_FOUND,
                message=outcome.message
                or "No catalogue content matches this question.",
                parse=outcome,
            )

        if outcome.status is ParseStatus.UNSUPPORTED:
            return AnswerResult(
                status=AnswerStatus.UNSUPPORTED,
                message=outcome.message
                or "This request type is not supported by the catalogue.",
                parse=outcome,
            )

        if outcome.status is ParseStatus.PARSE_ERROR:
            return AnswerResult(
                status=AnswerStatus.PARSE_ERROR,
                message=outcome.message + " Please rephrase the question.",
                parse=outcome,
            )

        intent = outcome.intent
        if intent is None:  # defensive; RESOLVED always carries an intent
            return AnswerResult(
                status=AnswerStatus.ERROR,
                message="Internal error: resolved parse without an intent.",
            )

        lookup = self.engine.lookup_by_intent(intent)

        if lookup.kind == "EXACT_MATCH":
            row = lookup.rows[0]
            if conversation_context is not None:
                conversation_context.record(
                    user_text, intent=intent, row_id=row.row_id, answered=True
                )
            return AnswerResult(
                status=AnswerStatus.ANSWERED,
                message=f"Exact catalogue match (row ID {row.row_id}).",
                answer_text=row.answer,
                question_text=row.question,
                row_id=row.row_id,
                metric=row.metric,
                request=row.request,
                scope=row.scope,
                unit=row.unit,
                frequency_selection=row.frequency_selection,
                answer_type=row.answer_type,
                intent=intent,
                parse=outcome,
            )

        if lookup.kind == "AMBIGUOUS":
            return AnswerResult(
                status=AnswerStatus.NEEDS_CLARIFICATION,
                message="This query matches multiple catalogue rows; "
                        "please narrow it down.",
                intent=intent,
                parse=outcome,
            )

        if conversation_context is not None:
            conversation_context.record(user_text, intent=intent, answered=False)
        return AnswerResult(
            status=AnswerStatus.NOT_FOUND,
            message="I understood the request, but no matching catalogue "
                    "record exists.",
            intent=intent,
            parse=outcome,
        )
