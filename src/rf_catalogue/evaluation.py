"""Evaluation harness: LLM parsing accuracy against the gold set.

Measures (milestone 2, requirement 9):
- intent parsing accuracy        (canonical 5-field dims == expected)
- exact row resolution accuracy  (lookup_by_intent row ID == expected)
- parameter extraction accuracy  (params key == expected)
- clarification accuracy         (status + missing fields)
- NOT_FOUND accuracy             (status + message)

Usage:
    python -m rf_catalogue.evaluation [--subset smoke|core|full]
                                      [--gold DIR] [--out report.json]

Requires a configured LLM client (OPENROUTER_API_KEY etc.).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.query_intent import QueryIntent

DEFAULT_GOLD_DIR = Path(r"C:\Ronni\Projects\astra chatbot\evaluation")

SUBSET_RANK = {"smoke": 0, "core": 1, "full": 2}


@dataclass
class ItemResult:
    id: str
    kind: str
    text: str
    expected: str
    actual: str
    passed: bool
    detail: str = ""


@dataclass
class EvaluationReport:
    subset: str
    total_items: int = 0
    # row / paraphrase items
    row_items: int = 0
    intent_parse_correct: int = 0
    row_resolution_correct: int = 0
    param_items: int = 0
    params_correct: int = 0
    # clarification items
    clarification_items: int = 0
    clarification_correct: int = 0
    # not_found items
    not_found_items: int = 0
    not_found_correct: int = 0
    # unsupported items
    unsupported_items: int = 0
    unsupported_correct: int = 0
    # per-dimension accuracy (row/paraphrase items with a resolved intent)
    dim: dict = field(default_factory=lambda: {
        d: {"correct": 0, "total": 0}
        for d in ("metric", "request", "scope", "frequency_selection", "unit")
    })
    # provider/budget failures (HTTP 402, timeouts): infrastructure, NOT
    # parser failures — excluded from all accuracy denominators
    provider_error_items: int = 0
    # false clarifications: non-clarification items the system sent to
    # clarification anyway (lower = better; drives precision below)
    false_clarifications: int = 0
    failures: list[ItemResult] = field(default_factory=list)

    def accuracy_row_resolution(self) -> float:
        return self.row_resolution_correct / self.row_items if self.row_items else 0.0

    def accuracy_intent_parse(self) -> float:
        return self.intent_parse_correct / self.row_items if self.row_items else 0.0

    def accuracy_params(self) -> float:
        return self.params_correct / self.param_items if self.param_items else 0.0

    def accuracy_clarification(self) -> float:
        return (self.clarification_correct / self.clarification_items
                if self.clarification_items else 0.0)

    def precision_clarification(self) -> float:
        """Of everything sent to clarification, the fraction that was genuinely
        ambiguous/missing per the gold labels (rest are false clarifications)."""
        sent = self.clarification_correct + self.false_clarifications
        return self.clarification_correct / sent if sent else 0.0

    def accuracy_not_found(self) -> float:
        return (self.not_found_correct / self.not_found_items
                if self.not_found_items else 0.0)

    def accuracy_unsupported(self) -> float:
        return (self.unsupported_correct / self.unsupported_items
                if self.unsupported_items else 0.0)

    def dimension_accuracy(self, dim: str) -> float:
        d = self.dim.get(dim, {"correct": 0, "total": 0})
        return d["correct"] / d["total"] if d["total"] else 0.0

    def to_dict(self) -> dict:
        return {
            "subset": self.subset,
            "total_items": self.total_items,
            "queryintent_exact_match_accuracy": self.accuracy_intent_parse(),
            "exact_row_resolution_accuracy": self.accuracy_row_resolution(),
            "metric_accuracy": self.dimension_accuracy("metric"),
            "request_accuracy": self.dimension_accuracy("request"),
            "scope_accuracy": self.dimension_accuracy("scope"),
            "unit_accuracy": self.dimension_accuracy("unit"),
            "frequency_selection_accuracy":
                self.dimension_accuracy("frequency_selection"),
            "parameter_extraction_accuracy": self.accuracy_params(),
            "clarification_accuracy": self.accuracy_clarification(),
            "clarification_precision": self.precision_clarification(),
            "not_found_accuracy": self.accuracy_not_found(),
            "unsupported_query_accuracy": self.accuracy_unsupported(),
            "provider_error_items_excluded": self.provider_error_items,
            "counts": {
                "row_items": self.row_items,
                "intent_parse_correct": self.intent_parse_correct,
                "row_resolution_correct": self.row_resolution_correct,
                "param_items": self.param_items,
                "params_correct": self.params_correct,
                "clarification_items": self.clarification_items,
                "clarification_correct": self.clarification_correct,
                "false_clarifications": self.false_clarifications,
                "not_found_items": self.not_found_items,
                "not_found_correct": self.not_found_correct,
                "unsupported_items": self.unsupported_items,
                "unsupported_correct": self.unsupported_correct,
            },
            "failures": [vars(f) for f in self.failures[:50]],
        }


def load_jsonl(path: str | Path) -> list[dict]:
    items = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _expected_intent_key(exp: dict) -> tuple:
    return (
        exp["metric"], exp["request"], exp["scope"], exp["frequency_selection"],
        exp["unit"], *tuple(exp["params"]),
    )


def evaluate(
    service,
    items: list[dict],
    subset: str = "core",
    progress_every: int = 25,
) -> EvaluationReport:
    rank = SUBSET_RANK[subset]
    report = EvaluationReport(subset=subset)
    selected = [it for it in items
                if SUBSET_RANK.get(it.get("subset", "core"), 1) <= rank]
    report.total_items = len(selected)

    for n, item in enumerate(selected, 1):
        kind = item["kind"]
        context = ConversationContext()
        result = service.answer_question(item["text"], context)

        # Provider/budget/infrastructure failures are excluded from every
        # accuracy denominator and reported separately.
        if result.status.name == "ERROR":
            report.provider_error_items += 1
            report.failures.append(ItemResult(
                id=item["id"], kind=kind, text=item["text"],
                expected="answered/classified",
                actual="ERROR", passed=False,
                detail=(result.message or "")[:120],
            ))
            continue

        if kind in ("row", "paraphrase"):
            report.row_items += 1
            expected_id = item["expected_row_id"]
            expected_key = _expected_intent_key(item["expected_intent"])
            if result.intent is not None:
                actual_key = result.intent.intent_key()
                if actual_key == tuple(expected_key):
                    report.intent_parse_correct += 1
                # per-dimension accuracy
                for i, dim in enumerate(("metric", "request", "scope",
                                         "frequency_selection", "unit")):
                    report.dim[dim]["total"] += 1
                    if actual_key[i] == expected_key[i]:
                        report.dim[dim]["correct"] += 1
                if result.status.name == "ANSWERED" and result.row_id == expected_id:
                    report.row_resolution_correct += 1
                else:
                    report.failures.append(ItemResult(
                        id=item["id"], kind=kind, text=item["text"],
                        expected=f"row {expected_id}",
                        actual=f"{result.status.name} row={result.row_id}",
                        passed=False,
                        detail=f"key mismatch={actual_key != tuple(expected_key)} "
                               f"{result.message[:120]}",
                    ))
            else:
                report.failures.append(ItemResult(
                    id=item["id"], kind=kind, text=item["text"],
                    expected=f"row {expected_id}",
                    actual=result.status.name, passed=False,
                    detail=result.message[:160],
                ))
            if item["expected_intent"]["params"] != ["none"]:
                report.param_items += 1
                if result.intent is not None and \
                        list(result.intent.params.key()) == item["expected_intent"]["params"]:
                    report.params_correct += 1

        elif kind in ("clarification",):
            report.clarification_items += 1
            ok = result.status.name == item["expect_status"]
            if ok and "expect_missing" in item:
                ok = set(item["expect_missing"]) <= set(result.missing_fields)
            if ok and "expect_missing_contains" in item:
                joined = " ".join(result.missing_fields)
                ok = all(token in joined
                         for token in item["expect_missing_contains"])
            if ok and "expect_candidates_contain" in item:
                candidate_requests = sorted({
                    c.request for c in result.parse.candidates
                }) if result.parse and result.parse.candidates else []
                ok = all(token in candidate_requests
                         for token in item["expect_candidates_contain"])
            if ok and "expect_hints_contain" in item:
                joined = " ".join(result.candidate_hints)
                ok = all(token in joined
                         for token in item["expect_hints_contain"])
            if ok:
                report.clarification_correct += 1
            else:
                report.failures.append(ItemResult(
                    id=item["id"], kind=kind, text=item["text"],
                    expected=f"{item['expect_status']} "
                             f"{item.get('expect_missing', item.get('expect_missing_contains'))}",
                    actual=f"{result.status.name} missing={result.missing_fields}",
                    passed=False,
                ))

        elif kind in ("not_found", "unsupported"):
            report.not_found_items += kind == "not_found"
            report.unsupported_items += kind == "unsupported"
            ok = result.status.name == item["expect_status"]
            if ok and "expect_message_contains" in item:
                ok = all(token in (result.message or "")
                         for token in item["expect_message_contains"])
            if ok:
                if kind == "not_found":
                    report.not_found_correct += 1
                else:
                    report.unsupported_correct += 1
            else:
                report.failures.append(ItemResult(
                    id=item["id"], kind=kind, text=item["text"],
                    expected=item["expect_status"],
                    actual=result.status.name, passed=False,
                    detail=(result.message or "")[:160],
                ))

        # false clarifications: resolvable/out-of-catalogue items that the
        # system failed to classify and punted to clarification instead
        if kind in ("row", "paraphrase", "not_found", "unsupported") \
                and result.status.name == "NEEDS_CLARIFICATION":
            report.false_clarifications += 1

        if progress_every and n % progress_every == 0:
            print(f"  ... {n}/{len(selected)} items evaluated", flush=True)

    return report


def print_report(report: EvaluationReport) -> None:
    print("=" * 64)
    print(f"EVALUATION REPORT (subset: {report.subset})")
    print("=" * 64)
    print(f"Total items                          : {report.total_items}")
    print(f"QueryIntent exact match accuracy     : "
          f"{report.accuracy_intent_parse():.1%} "
          f"({report.intent_parse_correct}/{report.row_items})")
    print(f"Exact row-resolution accuracy        : "
          f"{report.accuracy_row_resolution():.1%} "
          f"({report.row_resolution_correct}/{report.row_items})")
    print(f"Metric accuracy                      : "
          f"{report.dimension_accuracy('metric'):.1%} "
          f"({report.dim['metric']['correct']}/{report.dim['metric']['total']})")
    print(f"Request accuracy                     : "
          f"{report.dimension_accuracy('request'):.1%} "
          f"({report.dim['request']['correct']}/{report.dim['request']['total']})")
    print(f"Scope accuracy                       : "
          f"{report.dimension_accuracy('scope'):.1%} "
          f"({report.dim['scope']['correct']}/{report.dim['scope']['total']})")
    print(f"Unit accuracy                        : "
          f"{report.dimension_accuracy('unit'):.1%} "
          f"({report.dim['unit']['correct']}/{report.dim['unit']['total']})")
    print(f"Frequency-selection accuracy         : "
          f"{report.dimension_accuracy('frequency_selection'):.1%} "
          f"({report.dim['frequency_selection']['correct']}/"
          f"{report.dim['frequency_selection']['total']})")
    print(f"Parameter extraction accuracy        : "
          f"{report.accuracy_params():.1%} "
          f"({report.params_correct}/{report.param_items})")
    print(f"Clarification accuracy               : "
          f"{report.accuracy_clarification():.1%} "
          f"({report.clarification_correct}/{report.clarification_items})")
    print(f"Clarification precision              : "
          f"{report.precision_clarification():.1%} "
          f"(false clarifications: {report.false_clarifications})")
    print(f"NOT_FOUND accuracy                   : "
          f"{report.accuracy_not_found():.1%} "
          f"({report.not_found_correct}/{report.not_found_items})")
    print(f"Unsupported-query accuracy           : "
          f"{report.accuracy_unsupported():.1%} "
          f"({report.unsupported_correct}/{report.unsupported_items})")
    if report.provider_error_items:
        print(f"Provider/budget errors (excluded)    : "
              f"{report.provider_error_items}")
    if report.failures:
        print(f"\nFirst failures ({len(report.failures)} recorded):")
        for f in report.failures[:10]:
            print(f"  [{f.id}] {f.kind}: expected {f.expected}; "
                  f"got {f.actual} {('; ' + f.detail) if f.detail else ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subset", choices=["smoke", "core", "full", "manual"],
                    default="smoke",
                    help="smoke=15 audited anchors; core=+family coverage; "
                         "full=complete 417-row coverage; manual=30-case "
                         "acceptance suite (use with --offline for the "
                         "deterministic mode)")
    ap.add_argument("--offline", action="store_true",
                    help="manual subset only: run the acceptance suite with "
                         "mocked QueryIntent outputs (no network/API calls)")
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD_DIR)
    ap.add_argument("--out", type=Path, default=None,
                    help="write JSON report to this path")
    args = ap.parse_args(argv)

    if args.subset == "manual":
        from rf_catalogue import acceptance

        json_out = args.out or acceptance.DEFAULT_JSON_OUT
        return acceptance.run_manual_cli(
            engine=None, offline=args.offline,
            cases_path=acceptance.DEFAULT_CASES_PATH,
            json_out=json_out, md_out=acceptance.DEFAULT_MD_OUT)

    print("Loading catalogue engine...")
    engine = CatalogueEngine()
    try:
        from rf_catalogue.service import RfCatalogueService

        service = RfCatalogueService(engine=engine)
    except RuntimeError as exc:
        print(f"\nLLM client not configured: {exc}")
        print("Set LLM_PROVIDER + the matching key variable (e.g. "
              "LLM_PROVIDER=groq + GROQ_API_KEY, or OPENROUTER_API_KEY / "
              "OPENAI_API_KEY) and re-run.")
        return 2

    items = (
        load_jsonl(args.gold / "gold_set.jsonl")
        + load_jsonl(args.gold / "paraphrases.jsonl")
        + load_jsonl(args.gold / "clarifications.jsonl")
    )
    t0 = time.time()
    report = evaluate(service, items, subset=args.subset)
    elapsed = time.time() - t0
    print_report(report)
    print(f"\nElapsed: {elapsed:.0f}s for {report.total_items} items "
          f"(provider: {service.parser.llm.model})")

    if args.out:
        args.out.write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"JSON report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
