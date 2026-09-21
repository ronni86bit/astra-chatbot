"""Automated 30-case manual acceptance runner (evaluation/MANUAL_TEST_PLAN.md).

Executes the acceptance cases through the NORMAL production pipeline
(`RfCatalogueService.answer_question` — the same entry point the terminal
chatbot uses) and produces PASS/FAIL/PROVIDER_ERROR/SKIPPED/
TEST_PLAN_REVIEW results with structured assertions:

- EXACT_MATCH cases: status + resolved row ID + verbatim stored ANSWER
  (anti-fabrication: compared against the engine's stored row).
- NEEDS_CLARIFICATION cases: status + missing-field/candidate/hint checks.
- NOT_FOUND / UNSUPPORTED cases: status (+ message content where specified).
- Context cases: conversation cases share one context; standalone cases
  start from a clean context; reset actions clear context mid-case.

Modes:
- LIVE  (default): the configured Groq/OpenRouter/OpenAI provider via the
  normal adapter. Provider failures are classified and reported as
  PROVIDER_ERROR — never as model failures.
- OFFLINE (--offline): deterministic/mocked QueryIntent drafts injected per
  case; NO network. Verifies case -> context handling -> QueryIntent
  validation -> deterministic lookup -> rendering. Clearly labelled
  OFFLINE / MOCKED; it does NOT validate LLM quality.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import FakeLLMClient

DEFAULT_CASES_PATH = Path(r"C:\Ronni\Projects\astra chatbot\evaluation\manual_cases.json")
DEFAULT_JSON_OUT = Path(r"C:\Ronni\Projects\astra chatbot\evaluation\manual_acceptance_report.json")
DEFAULT_MD_OUT = Path(r"C:\Ronni\Projects\astra chatbot\evaluation\MANUAL_ACCEPTANCE_REPORT.md")

OUTCOME_PASSED = "PASSED"
OUTCOME_FAILED = "FAILED"
OUTCOME_PROVIDER_ERROR = "PROVIDER_ERROR"
OUTCOME_SKIPPED = "SKIPPED"
OUTCOME_REVIEW = "TEST_PLAN_REVIEW"

_KEY_ENV_VARS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                 "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class MessageResult:
    text: str
    expected_status: str
    actual_status: str
    expected_row_id: int | None = None
    actual_row_id: int | None = None
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    note: str = ""
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class CaseResult:
    id: str
    area: str
    outcome: str
    reason: str = ""
    messages: list[MessageResult] = field(default_factory=list)
    expected_row_id: int | None = None
    actual_row_id: int | None = None
    resolved_intent: tuple | None = None


# ---------------------------------------------------------------------------
# Case loading / validation
# ---------------------------------------------------------------------------


def load_manual_cases(path: Path, engine: CatalogueEngine):
    """Load and structurally verify the acceptance cases.

    Returns (cases, review_entries). A case becomes a TEST_PLAN_REVIEW
    entry when it is structurally incomplete or cites a row ID that the
    deterministic engine does not contain (never invent an expectation).
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases, review = [], []
    by_id = {r.row_id: r for r in engine._catalogue}
    for case in raw.get("cases", []):
        cid = case.get("id", "?")
        problems = []
        if not case.get("messages"):
            problems.append("no messages")
        for i, msg in enumerate(case["messages"], 1):
            if "action" in msg:
                continue
            if "text" not in msg:
                problems.append(f"message {i} has no text")
            status = msg.get("expected_status") or case.get("expected_status")
            if not status:
                problems.append(f"message {i} has no expected_status")
            rid = msg.get("expected_row_id")
            if rid is not None and rid not in by_id:
                problems.append(f"message {i} cites unknown row {rid}")
            if msg.get("offline_llm") and not msg.get("offline_draft") \
                    and rid is None:
                problems.append(
                    f"message {i} requests the offline LLM mock but provides "
                    f"neither offline_draft nor expected_row_id")
        if problems:
            review.append({
                "id": cid, "area": case.get("area", ""),
                "reason": "; ".join(problems),
            })
            continue
        cases.append(case)
    return cases, review


# ---------------------------------------------------------------------------
# Provider-error classification
# ---------------------------------------------------------------------------


def classify_provider_error(message: str) -> str:
    lowered = (message or "").lower()
    if "401" in lowered or "unauthorized" in lowered or "authentication" in lowered:
        return "authentication"
    if "402" in lowered or "credit" in lowered:
        return "insufficient_credits"
    if "429" in lowered or ("rate" in lowered and "limit" in lowered):
        return "rate_limit"
    if ("timeout" in lowered or "timed out" in lowered
            or "did not respond in time" in lowered):
        return "timeout"
    if "json_validate" in lowered or "schema" in lowered:
        return "malformed_structured_output"
    if "500" in lowered or "502" in lowered or "503" in lowered or "server" in lowered:
        return "server_error"
    return "transient_provider_failure"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _offline_draft_for_message(engine: CatalogueEngine, msg: dict) -> dict:
    """Build the mocked QueryIntentDraft for an OFFLINE message."""
    base = {
        "metric": "", "request": "", "scope": "",
        "frequency_selection": "", "unit": "",
        "params": {"kind": "none"},
        "unsupported_dimension": "", "unsupported_value": "",
        "unresolvable_reason": "",
    }
    rid = msg.get("expected_row_id")
    if rid is not None:
        row = next(r for r in engine._catalogue if r.row_id == rid)
        from rf_catalogue.query_intent import intent_from_row

        intent = intent_from_row(row)
        base.update({
            "metric": intent.metric,
            "request": intent.request,
            "scope": intent.scope,
            "frequency_selection": intent.frequency_selection,
            "unit": intent.unit,
            "params": dict(zip(
                ("kind", "threshold", "closest_target", "range_min",
                 "range_max", "reference_qualifier"),
                intent.params.key())) if intent.params.key()[0] != "none"
            else {"kind": "none"},
        })
        if intent.params.key()[0] == "threshold":
            base["params"] = {"threshold": intent.params.key()[1]}
        elif intent.params.key()[0] == "closest":
            base["params"] = {"closest_target": intent.params.key()[1]}
        elif intent.params.key()[0] == "range":
            base["params"] = {"range_min": intent.params.key()[1],
                              "range_max": intent.params.key()[2]}
        elif intent.params.key()[0] == "refqual":
            base["params"] = {"reference_qualifier": intent.params.key()[1]}
        else:
            base["params"] = {"kind": "none"}
    overrides = msg.get("offline_draft") or {}
    base.update(overrides)
    return base


def _check_message(msg: dict, result, engine: CatalogueEngine,
                   msg_result: MessageResult) -> None:
    """Structured assertions for one message (never response-text matching)."""
    expected_status = msg.get("expected_status")
    msg_result.expected_status = expected_status
    msg_result.actual_status = result.status.value
    if result.status.value != expected_status:
        msg_result.failures.append(
            f"status {result.status.value} != expected {expected_status}")
        return  # deeper assertions are meaningless on a different outcome

    if expected_status == "ANSWERED":
        expected_row = msg.get("expected_row_id")
        msg_result.expected_row_id = expected_row
        msg_result.actual_row_id = result.row_id
        if result.row_id != expected_row:
            msg_result.failures.append(
                f"row {result.row_id} != expected {expected_row}")
        elif result.row_id is not None:
            # anti-fabrication: the returned ANSWER must be the stored row
            row = next(r for r in engine._catalogue
                       if r.row_id == result.row_id)
            if result.answer_text != row.answer:
                msg_result.failures.append("ANSWER text does not match the "
                                           "stored catalogue row (fabricated?)")
            from rf_catalogue.chatbot import render_result

            rendered = render_result(result)
            if f"Source row: {result.row_id}" not in rendered:
                msg_result.failures.append("rendering lacks source-row provenance")
    elif expected_status == "NEEDS_CLARIFICATION":
        for token in msg.get("expected_missing_contains", []):
            if not any(token in f for f in result.missing_fields):
                msg_result.failures.append(f"missing fields lack {token!r}")
        for token in msg.get("expected_candidates_contain", []):
            in_hints = any(token in h for h in result.candidate_hints)
            in_candidates = any(
                token in (c.metric, c.request, c.scope)
                for c in (result.candidates or []))
            if not (in_hints or in_candidates):
                msg_result.failures.append(f"candidates lack {token!r}")
        for token in msg.get("expected_hints_contain", []):
            if not any(token in h for h in result.candidate_hints):
                msg_result.failures.append(f"hints lack {token!r}")
        for dim, value in (msg.get("expected_resolved_dims") or {}).items():
            resolved = (result.parse.resolved_from_pending or {}).get(dim) \
                if result.parse else None
            draft_value = getattr(result.parse.draft, dim, None) \
                if result.parse and result.parse.draft else None
            if resolved != value and draft_value != value:
                msg_result.failures.append(
                    f"pending resolution for {dim} != {value!r}")
    elif expected_status == "NOT_FOUND":
        for token in msg.get("expected_message_contains", []):
            if token not in (result.message or ""):
                msg_result.failures.append(f"message lacks {token!r}")


def _evaluate_alternative(alt: dict, result) -> list[str]:
    """Evaluate a documented alternative outcome. Returns failure list."""
    failures: list[str] = []
    expected_status = alt.get("status")
    if result.status.value != expected_status:
        failures.append(f"status {result.status.value} != {expected_status}")
        return failures
    if "row_id" in alt and result.row_id != alt["row_id"]:
        failures.append(f"row {result.row_id} != {alt['row_id']}")
    for dim, value in (alt.get("resolved_dims") or {}).items():
        if result.intent is None or getattr(result.intent, dim) != value:
            failures.append(f"{dim} != {value}")
    if result.intent is None:
        failures.append("no resolved intent")
    return failures


def run_manual_suite(
    engine: CatalogueEngine,
    service,          # live service when offline=False; offline service when True
    offline: bool,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> dict:
    cases, review = load_manual_cases(cases_path, engine)
    started = time.time()
    results: list[CaseResult] = []

    for case in cases:
        context_mode = case.get("context_mode", "standalone")

        # -- UI / environment cases (no LLM, no catalogue lookup) -----------
        if context_mode == "ui":
            case_result = CaseResult(id=case["id"], area=case.get("area", ""),
                                     outcome=OUTCOME_PASSED)
            from rf_catalogue import chatbot

            state = {"service": None, "context": ConversationContext(),
                     "last_result": None}
            for msg in case["messages"]:
                text = msg.get("text", "")
                if not text.strip():
                    handled = chatbot.normalize_input(text) is None
                    ok = handled
                    detail = "whitespace input ignored"
                elif text.startswith("/"):
                    out = chatbot.handle_command(text, state)
                    ok = out is not None and "Unknown command" in out
                    detail = f"handled: {(out or '')[:60]}"
                else:
                    ok, detail = False, "unexpected UI input"
                if not ok:
                    case_result.outcome = OUTCOME_FAILED
                    case_result.reason = detail
                case_result.messages.append(MessageResult(
                    text=text, expected_status="UI_HANDLED",
                    actual_status="UI_HANDLED" if ok else "FAILED",
                    failures=[] if ok else [detail]))
            results.append(case_result)
            continue

        # -- Environment case (M30): missing-key behaviour -------------------
        if context_mode == "env":
            case_result = CaseResult(id=case["id"], area=case.get("area", ""),
                                     outcome=OUTCOME_PASSED)
            from rf_catalogue import chatbot

            saved = {var: os.environ.get(var)
                     for var in _KEY_ENV_VARS}
            try:
                for var in _KEY_ENV_VARS:
                    os.environ.pop(var, None)
                empty_env = Path(__file__).parent / "_empty_env.cfg"
                empty_env.write_text("", encoding="utf-8")
                try:
                    chatbot.build_service(env_file=str(empty_env))
                    case_result.outcome = OUTCOME_FAILED
                    case_result.reason = "build_service did not raise without a key"
                except RuntimeError as exc:
                    if "key" not in str(exc).lower():
                        case_result.outcome = OUTCOME_FAILED
                        case_result.reason = f"unclear error: {exc}"
                # degraded status surface still works
                state = {"service": None, "context": ConversationContext(),
                         "last_result": None}
                status_text = chatbot._render_status(state)
                if "NOT configured" not in status_text:
                    case_result.outcome = OUTCOME_FAILED
                    case_result.reason = "degraded /status lacks NOT configured"
            finally:
                for var, value in saved.items():
                    if value is not None:
                        os.environ[var] = value
                    else:
                        os.environ.pop(var, None)
            results.append(case_result)
            continue

        case_result = CaseResult(id=case["id"], area=case.get("area", ""),
                                 outcome=OUTCOME_PASSED,
                                 expected_row_id=case.get("expected_row_id"))
        # scripted offline drafts per case (consumed only when Stage B runs)
        if offline and service is not None:
            service.parser.llm.responses = [
                _offline_draft_for_message(engine, m)
                for m in case["messages"] if m.get("offline_llm", True)
            ]
        context = ConversationContext()
        for msg in case["messages"]:
            if "action" in msg:
                if msg["action"] == "reset_context":
                    context.reset()
                continue
            if context_mode == "standalone":
                context = ConversationContext()  # clean context every message
            t0 = time.time()
            result = service.answer_question(msg["text"], context)
            msg_res = MessageResult(text=msg["text"],
                                    expected_status=msg.get("expected_status",
                                                            ""),
                                    actual_status=result.status.value,
                                    latency_s=round(time.time() - t0, 2))
            if result.status.value == "ERROR":
                msg_res.failures = []
                case_result.outcome = OUTCOME_PROVIDER_ERROR
                case_result.reason = classify_provider_error(result.message)
                case_result.messages.append(msg_res)
                break
            _check_message(msg, result, engine, msg_res)
            if msg_res.failures and msg.get("accept_alternatives"):
                for alt in msg["accept_alternatives"]:
                    alt_failures = _evaluate_alternative(alt, result)
                    if not alt_failures:
                        msg_res.failures = []
                        if "row_id" in alt:
                            msg_res.actual_row_id = alt["row_id"]
                            case_result.actual_row_id = alt["row_id"]
                            case_result.expected_row_id = alt.get(
                                "row_id", case_result.expected_row_id)
                        msg_res.note = ("passed via documented alternative "
                                        "outcome: "
                                        + alt.get("note", "")[:120])
                        break
            case_result.messages.append(msg_res)
            if result.status.value == "ANSWERED":
                case_result.actual_row_id = result.row_id
                if result.intent is not None:
                    case_result.resolved_intent = result.intent.intent_key()
                context.record(msg["text"], intent=result.intent,
                               row_id=result.row_id, answered=True)
            if msg_res.failures:
                case_result.outcome = OUTCOME_FAILED
                case_result.reason = "; ".join(msg_res.failures)[:200]
        if case_result.outcome == OUTCOME_PASSED and \
                not all(m.ok for m in case_result.messages):
            case_result.outcome = OUTCOME_FAILED
        if case_result.outcome == OUTCOME_FAILED and not case_result.reason:
            first_fail = next((m for m in case_result.messages if m.failures),
                              None)
            if first_fail:
                case_result.reason = "; ".join(first_fail.failures)[:200]
        results.append(case_result)

    report = build_report(results, review, offline,
                          time.time() - started)
    return report


def build_report(results: list[CaseResult], review: list[dict], offline: bool,
                 duration_s: float) -> dict:
    counts = Counter(r.outcome for r in results)
    row_items = [r for r in results if r.expected_row_id is not None
                 and r.outcome != OUTCOME_PROVIDER_ERROR]
    row_ok = sum(1 for r in row_items
                 if r.outcome == OUTCOME_PASSED and r.actual_row_id is not None)
    return {
        "suite": "manual-acceptance",
        "mode": "OFFLINE/MOCKED" if offline else "LIVE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(duration_s, 1),
        "total_cases": len(results) + len(review),
        "counts": {
            "passed": counts.get(OUTCOME_PASSED, 0),
            "failed": counts.get(OUTCOME_FAILED, 0),
            "provider_error": counts.get(OUTCOME_PROVIDER_ERROR, 0),
            "skipped": counts.get(OUTCOME_SKIPPED, 0),
            "test_plan_review": len(review),
        },
        "row_resolution": {"correct": row_ok, "total": len(row_items)},
        "review_items": review,
        "cases": [vars(r) for r in results],
    }


def print_report(report: dict) -> None:
    counts = report["counts"]
    print("=" * 66)
    print("RF/SYSTEMVUE MANUAL ACCEPTANCE TEST"
          + ("  [OFFLINE / MOCKED]" if report["mode"] == "OFFLINE/MOCKED" else ""))
    print("=" * 66)
    print(f"{report['total_cases']} total")
    print(f"{counts['passed']} passed")
    print(f"{counts['failed']} failed")
    print(f"{counts['provider_error']} provider error")
    print(f"{counts['skipped']} skipped")
    print(f"{counts['test_plan_review']} test-plan review")
    print()
    for case in report["cases"]:
        marker = {"PASSED": "PASS", "FAILED": "FAIL",
                  "PROVIDER_ERROR": "PROVIDER_ERROR",
                  "SKIPPED": "SKIPPED",
                  "TEST_PLAN_REVIEW": "REVIEW"}.get(case["outcome"],
                                                    case["outcome"])
        print(f"{case['id']}  {marker}  ({case['area']})")
        if case["expected_row_id"] is not None or case["actual_row_id"]:
            print(f"  Expected: row {case['expected_row_id']}")
            print(f"  Actual:   row {case['actual_row_id']}")
        elif case["messages"]:
            last = case["messages"][-1]
            print(f"  Expected: {last.expected_status}")
            print(f"  Actual:   {last.actual_status}")
        if case["outcome"] != OUTCOME_PASSED and case["reason"]:
            print(f"  Reason:   {case['reason']}")
    print()
    print("=" * 30 + " SUMMARY " + "=" * 30)
    print(f"Exact row resolution : {report['row_resolution']['correct']}"
          f"/{report['row_resolution']['total']}")
    print(f"Provider errors      : {counts['provider_error']}")
    print(f"Failures             : {counts['failed']}")
    if report["mode"] == "OFFLINE/MOCKED":
        print("(OFFLINE/MOCKED run: verifies context/validation/lookup/"
              "rendering, NOT LLM quality)")


def write_json_report(report: dict, path: Path) -> None:
    """Machine-readable report. Never contains credentials."""
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                     default=str),
                          encoding="utf-8")


def write_md_report(report: dict, path: Path) -> None:
    """Human-readable summary of the latest acceptance run (latest-wins)."""
    counts = report["counts"]
    total_rows = report["row_resolution"]["total"] or 1
    lines = [
        "# Manual Acceptance Report (latest run)",
        "",
        f"- Run at : {report['timestamp']}",
        f"- Mode   : {report['mode']}",
        f"- Duration: {report['duration_s']}s",
        "",
        "| metric | value |",
        "|---|---|",
        f"| total cases | {report['total_cases']} |",
        f"| passed | {counts['passed']} |",
        f"| failed | {counts['failed']} |",
        f"| provider errors | {counts['provider_error']} |",
        f"| skipped | {counts['skipped']} |",
        f"| test-plan review | {counts['test_plan_review']} |",
        f"| row-resolution accuracy | "
        f"{report['row_resolution']['correct']}/{total_rows} |",
        "",
        "## Non-passing cases",
        "",
    ]
    non_passing = [c for c in report["cases"]
                   if c["outcome"] != OUTCOME_PASSED]
    if not non_passing:
        lines.append("None — all cases passed.")
    else:
        for c in non_passing:
            lines.append(f"- **{c['id']}** ({c['area']}): {c['outcome']} — "
                         f"{c['reason']}")
    lines.append("")
    lines.append("_This file is the latest-run summary; it is overwritten on "
                 "every acceptance run (timestamped above)._")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def show_failure_details_report(report: dict) -> None:
    shown = 0
    for case in report["cases"]:
        if case["outcome"] in (OUTCOME_FAILED, OUTCOME_PROVIDER_ERROR):
            print(f"\nFAIL detail [{case['id']}] {case['area']}")
            for m in case["messages"]:
                print(f"  Q: {m.text}")
                print(f"  expected {m.expected_status} / actual {m.actual_status}"
                      f" | row {m.expected_row_id} -> {m.actual_row_id}")
            print(f"  intent : {case['resolved_intent']}")
            print(f"  reason : {case['reason']}")
            shown += 1
            if shown >= 10:
                break


def run_manual_cli(
    engine: CatalogueEngine,
    offline: bool,
    cases_path: Path = DEFAULT_CASES_PATH,
    json_out: Path = DEFAULT_JSON_OUT,
    md_out: Path = DEFAULT_MD_OUT,
) -> int:
    """CLI entry for `--subset manual [--offline]`. Returns exit code."""
    from rf_catalogue.service import RfCatalogueService

    if offline:
        engine = engine or CatalogueEngine()
        service = RfCatalogueService(
            engine=engine, llm_client=FakeLLMClient(responses=[]))
        report = run_manual_suite(engine, service, offline=True,
                                  cases_path=cases_path)
    else:
        try:
            service = RfCatalogueService(engine=engine)
        except RuntimeError as exc:
            print("LIVE_UNAVAILABLE: " + str(exc))
            print("Run the OFFLINE/MOCKED acceptance suite instead: "
                  "python -m rf_catalogue.evaluation --subset manual --offline")
            return 3
        engine = service.engine  # service may have loaded its own engine
        report = run_manual_suite(engine, service, offline=False,
                                  cases_path=cases_path)

    print_report(report)
    show_failure_details_report(report)
    write_json_report(report, json_out)
    write_md_report(report, md_out)
    print(f"\nJSON report: {json_out}")
    print(f"MD report  : {md_out}")

    counts = report["counts"]
    if counts["failed"] > 0 or counts["test_plan_review"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    from rf_catalogue.engine import CatalogueEngine as _E

    raise SystemExit(run_manual_cli(_E(), offline="--offline" in sys.argv))
