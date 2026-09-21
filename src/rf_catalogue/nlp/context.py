"""Lightweight conversation context.

Design-only at this milestone: carries the last resolved intent's dimensions
so a follow-up like "What about the best?" can inherit the stable dimensions
(metric, scope, unit, frequency_selection) while the changed dimension
(request, params) comes from the new message.

Conservative rules:
- Only dimensions the NEW draft left null may be inherited.
- request and params are NEVER inherited (a follow-up normally changes them).
- If inheritance still leaves required dimensions null, the parser must
  ask for clarification — never guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from rf_catalogue.query_intent import QueryIntent

#: dimensions eligible for inheritance from prior turns
INHERITABLE_DIMENSIONS = ("metric", "scope", "unit", "frequency_selection")


@dataclass
class ConversationTurn:
    user_text: str
    intent: QueryIntent | None
    row_id: int | None
    answered: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationContext:
    """Minimal turn history with conservative dimension inheritance.

    Also carries ONE pending clarification: when the parser asks the user to
    disambiguate (e.g. "GAIN or CGAIN?"), the unfinished draft is stored so
    the user's short reply ("CGAIN") can complete it without re-parsing the
    question from scratch. Structured — never raw conversation stuffing.
    """

    def __init__(self) -> None:
        self.turns: list[ConversationTurn] = []
        self.pending: dict | None = None   # {"draft": {...}, "missing": [...]}

    def record(
        self,
        user_text: str,
        intent: QueryIntent | None = None,
        row_id: int | None = None,
        answered: bool = False,
    ) -> ConversationTurn:
        turn = ConversationTurn(
            user_text=user_text, intent=intent, row_id=row_id, answered=answered
        )
        self.turns.append(turn)
        return turn

    def set_pending(self, draft: dict, missing: list[str]) -> None:
        """Store an unfinished draft awaiting clarification."""
        self.pending = {"draft": dict(draft), "missing": list(missing)}

    def clear_pending(self) -> None:
        self.pending = None

    @property
    def last_answered_intent(self) -> QueryIntent | None:
        for turn in reversed(self.turns):
            if turn.answered and turn.intent is not None:
                return turn.intent
        return None

    def inherit(self, draft_null_dimensions: set[str]) -> dict[str, str]:
        """Return inheritable dimension values from the last answered intent.

        Only dimensions present in `draft_null_dimensions` (i.e. the new
        message did not determine them) are inherited, and only from the
        most recent ANSWERED turn.
        """
        intent = self.last_answered_intent
        if intent is None:
            return {}
        inherited: dict[str, str] = {}
        for dim in INHERITABLE_DIMENSIONS:
            if dim in draft_null_dimensions:
                inherited[dim] = getattr(intent, dim)
        return inherited

    def summary_for_prompt(self) -> str:
        """Compact, factual context line for the LLM prompt (no reasoning)."""
        intent = self.last_answered_intent
        if intent is None:
            return "none"
        return (
            f"metric={intent.metric}; request={intent.request}; "
            f"scope={intent.scope}; frequency={intent.frequency_selection}; "
            f"unit={intent.unit}"
        )

    def reset(self) -> None:
        self.turns.clear()
        self.pending = None
