"""RF/SystemVue Question Assistant - terminal chatbot.

Run:
    python -m rf_catalogue.chatbot

Natural-language questions are answered with the exact stored catalogue row
(citing its row ID). The LLM only interprets the question; the deterministic
catalogue engine is the source of truth for every answer.

Commands: /help /status /debug /examples /reset /quit
"""

from __future__ import annotations

import sys

from rf_catalogue.nlp.context import ConversationContext

_BANNER = """\
RF/SystemVue Question Assistant
--------------------------------
Ask natural-language questions about the RF catalogue and receive the exact
answer stored in the SystemVue workbook (answers cite their catalogue row).

Examples:
  What is the worst Cascaded Gain in the final node?
  Where is Mismatch Loss above 0.5 dB?
  Show Mismatch Loss for part ADL8124 across frequency.

Commands: /help /status /debug /examples /reset /quit
"""

_HELP = """\
Commands:
  /help     Show this help.
  /status   Provider, model, catalogue size, LLM availability, context.
  /debug    Show the most recent resolved QueryIntent and status (no secrets).
  /examples Show representative valid questions from the catalogue.
  /reset    Clear conversation context.
  /quit     Exit cleanly (also /exit, /q, Ctrl+C).

Usage tips:
  - Metrics: Cascaded Gain (CGAIN), Stage Power Gain (GAIN), Mismatch Loss,
    Desired Channel Power (DCP), Channel Power (CP), Channel Noise Power
    (CNP), Total Node Power (TNP), Cascaded Compression (CCOMP), ...
  - Frequencies: 1200-3000 MHz in 100 MHz steps, or "All Frequencies".
  - Threshold/closest/range values belong in the question, e.g.
    "Where is Mismatch Loss above 0.5 dB?"
  - If something is ambiguous or missing, the assistant asks you to
    clarify instead of guessing.
"""

_NO_SERVICE_QUESTION = (
    "The LLM interpretation layer is not available, so questions cannot be "
    "answered right now.\n"
    "Configure your provider (LLM_PROVIDER and the matching API key, e.g. "
    "GROQ_API_KEY) in .env (see .env.example) and restart, or use /status "
    "for details."
)


def render_result(result) -> str:
    """Render an AnswerResult for the terminal (concise, provenance-cited)."""
    from rf_catalogue.service import AnswerStatus

    if result.status == AnswerStatus.ANSWERED:
        lines = ["Answer:", result.answer_text or ""]
        lines.append("")
        lines.append(f"Source row: {result.row_id}")
        if result.question_text:
            lines.append(f"Catalogue question: {result.question_text}")
        if result.intent is not None:
            lines.append("")
            lines.append("Resolved intent:")
            lines.append(f"Metric: {result.intent.metric}")
            lines.append(f"Request: {result.intent.request}")
            lines.append(f"Scope: {result.intent.scope}")
            lines.append(f"Frequency: {result.intent.frequency_selection}")
            lines.append(f"Unit: {result.intent.unit}")
            params_key = result.intent.params.key()
            if params_key[0] != "none":
                lines.append(f"Parameters: {params_key[1:]}")
        return "\n".join(lines)

    if result.status == AnswerStatus.NEEDS_CLARIFICATION:
        lines = [result.message or "I need more information."]
        if result.candidate_hints:
            lines.extend(f" - {hint}" for hint in result.candidate_hints)
        return "\n".join(lines)

    if result.status == AnswerStatus.NOT_FOUND:
        lines = [result.message or
                 "I understood the request, but no matching catalogue record "
                 "exists."]
        lines.append("(NOT_FOUND)")
        if result.intent is not None:
            lines.append(f"Interpreted as: {result.intent.intent_key()}")
        return "\n".join(lines)

    if result.status == AnswerStatus.UNSUPPORTED:
        return (result.message or
                "That capability is outside the current catalogue.") + \
            " (UNSUPPORTED)"

    if result.status == AnswerStatus.PARSE_ERROR:
        return result.message or (
            "I could not reliably interpret that question. "
            "Please rephrase it."
        )

    return result.message or "Something went wrong."


def normalize_input(text: str | None) -> str | None:
    """Normalize raw terminal input: None/whitespace-only -> None (re-prompt)."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def handle_command(command: str, state: dict) -> str | None:
    """Handle a /command. Returns text to print, or None to exit.

    `state` carries {"service", "context", "last_result"}.
    """
    cmd = command.strip().lower()
    context: ConversationContext = state["context"]

    if cmd in ("/quit", "/exit", "/q"):
        return None
    if cmd == "/help":
        return _HELP
    if cmd == "/reset":
        context.reset()
        state["last_result"] = None
        return "Context cleared."
    if cmd == "/status":
        return _render_status(state)
    if cmd == "/debug":
        return _render_debug(state)
    if cmd == "/examples":
        return _render_examples(state)
    return f"Unknown command: {command} (try /help)"


def _render_status(state: dict) -> str:
    service = state["service"]
    lines = []
    if service is None:
        lines.append("Status:")
        lines.append("  catalogue           : not loaded (LLM key missing)")
        lines.append("  live mode           : NOT configured")
        lines.append("  context             : "
                     + state["context"].summary_for_prompt())
        lines.append("  fix                 : set OPENROUTER_API_KEY in .env "
                     "and restart")
        return "\n".join(lines)

    engine = service.engine
    lines = ["Status:",
             f"  catalogue rows      : {engine.total_rows()}",
             f"  unique 5-field keys : {engine.unique_lookup_keys()}",
             f"  intent keys         : {engine.intent_unique_keys()} "
             f"(duplicates: {engine.intent_duplicate_keys()})"]
    client = getattr(service.parser, "llm", None)
    model = getattr(client, "model", None)
    provider = getattr(client, "provider", None)
    lines.append(f"  provider            : {provider or 'n/a'}")
    lines.append(f"  model               : {model or 'n/a'}")
    base_url = getattr(getattr(client, "_client", None), "base_url", None)
    lines.append(f"  provider endpoint   : {base_url or 'n/a'}")
    lines.append("  live mode           : "
                 + ("active" if model else "NOT configured (set OPENROUTER_API_KEY)"))
    lines.append(f"  context             : {state['context'].summary_for_prompt()}")
    if state["context"].pending:
        lines.append(f"  pending clarification: missing "
                     f"{state['context'].pending['missing']}")
    return "\n".join(lines)


def _render_debug(state: dict) -> str:
    result = state.get("last_result")
    if result is None:
        return "No question answered yet in this session."
    lines = ["Last result (no secrets):",
             f"  status : {result.status.value}"]
    if result.intent is not None:
        lines.append(f"  intent : {result.intent.intent_key()}")
        inferred = getattr(result.parse, "inferred", {}) if result.parse else {}
        if inferred:
            lines.append(f"  inferred: {inferred}")
        resolved = getattr(result.parse, "resolved_from_pending", {}) \
            if result.parse else {}
        if resolved:
            lines.append(f"  from clarification: {resolved}")
    if result.row_id is not None:
        lines.append(f"  row    : {result.row_id}")
    if result.missing_fields:
        lines.append(f"  missing: {result.missing_fields}")
    return "\n".join(lines)


def _render_examples(state: dict) -> str:
    service = state["service"]
    if service is None:
        return "\n".join(f"  - {q}" for q in [
            "What is the best Mismatch Loss in the final node?",
            "Where is Mismatch Loss above 0.5 dB?",
        ])
    engine = service.engine
    wanted = [1, 10, 2850, 3227, 178, 4772]
    by_id = {r.row_id: r for r in engine._catalogue}
    lines = ["Representative catalogue questions:"]
    for rid in wanted:
        row = by_id.get(rid)
        if row:
            lines.append(f"  - {row.question}")
    return "\n".join(lines)


def build_service(env_file: str | None = None):
    """Construct the service; raises RuntimeError when no key is configured."""
    from rf_catalogue.service import RfCatalogueService

    return RfCatalogueService(env_file=env_file)


def main() -> int:
    print(_BANNER)
    print("Loading catalogue workbook (this takes a few seconds)...")

    state = {"service": None,
             "context": ConversationContext(),
             "last_result": None}
    try:
        state["service"] = build_service()
    except RuntimeError as exc:
        # Degraded mode: the session still starts so the user can inspect
        # commands, but questions require the LLM interpretation layer.
        print(f"\nLLM layer unavailable: {exc}")
        print("Starting in limited mode - questions cannot be answered until "
              "the key is configured.\n")
    except Exception as exc:  # catalogue load error etc.
        print(f"\nFailed to initialise: {exc}")
        return 2

    if state["service"] is not None:
        print("Ready. Type your question.\n")

    while True:
        try:
            user_text = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        user_text = normalize_input(user_text)
        if not user_text:
            continue

        if user_text.startswith("/"):
            try:
                out = handle_command(user_text, state)
            except Exception as exc:  # never crash on a command
                out = f"Command failed: {exc}"
            if out is None:
                print("Goodbye.")
                return 0
            print(out)
            continue

        if state["service"] is None:
            print(f"Assistant: {_NO_SERVICE_QUESTION}\n")
            continue

        try:
            result = state["service"].answer_question(user_text,
                                                      state["context"])
        except Exception as exc:  # absolute last-resort guard; never crash
            print(f"Assistant: Sorry - an unexpected error occurred "
                  f"({type(exc).__name__}). Please try again.\n")
            continue
        state["last_result"] = result
        print(f"Assistant: {render_result(result)}\n")


if __name__ == "__main__":
    sys.exit(main())
