"""Build the gold evaluation set from REAL workbook rows (deterministic).

Outputs (JSONL):
    evaluation/gold_set.jsonl
        {"id", "subset": "smoke|core|full", "kind": "row", "text": <verbatim QUESTION>,
         "expected_row_id", "expected_intent": {5 dims + params}}
    evaluation/paraphrases.jsonl
        {"id", "kind": "paraphrase", "text": <curated phrasing>, "expected_row_id",
         "expected_intent": {...}}
    evaluation/clarifications.jsonl
        {"id", "kind": "clarification"|"not_found", "text", "expect_status",
         "expect_missing"?: [...], "expect_message_contains"?: [...]}

Every expectation is verified against the catalogue engine BEFORE writing:
a gold item whose expected row ID / intent cannot be reproduced by the
trusted deterministic pipeline fails the build (no blind generation).

Run:  python evaluation/build_gold_set.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Ronni\Projects\astra chatbot\src")

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.query_intent import intent_from_row

OUT_DIR = Path(r"C:\Ronni\Projects\astra chatbot\evaluation")


def intent_dict(row) -> dict:
    intent = intent_from_row(row)
    return {
        "metric": intent.metric,
        "request": intent.request,
        "scope": intent.scope,
        "frequency_selection": intent.frequency_selection,
        "unit": intent.unit,
        "params": list(intent.params.key()),
    }


def key_of(row) -> tuple:
    return (row.metric, row.request, row.scope, row.frequency_selection, row.unit)


def select_dup_rows(engine: CatalogueEngine) -> tuple[list[dict], dict[str, int]]:
    """All 417 rows of the 80 audited duplicate 5-field keys, each emitted
    EXACTLY ONCE with its highest-priority subset label:

    smoke = audited regression anchors (threshold 0.1 vs 10 dB, closest
            targets, range bounds, desired vs channel power)
    core  = all rows of the first-seen duplicate key per (metric, request)
            family (full family coverage)
    full  = remaining rows (together: complete 417-row coverage)
    """
    anchors = {
        2850, 2868,                      # threshold 0.1 dB vs 10 dB
        3071, 3074, 3077, 3080, 3083,    # closest targets
        3084, 3085, 3086, 3087,          # range bounds
        2421, 2423, 4772, 4774,          # desired vs channel power
    }
    items: list[dict] = []
    counts = {"smoke": 0, "core": 0, "full": 0}
    seen_families: set[tuple] = set()
    for key5, rows in sorted(engine._index.items()):
        if len(rows) <= 1:
            continue
        family = (key5[0], key5[1])
        family_first = family not in seen_families
        seen_families.add(family)
        for row in rows:
            if row.row_id in anchors:
                subset = "smoke"
            elif family_first:
                subset = "core"
            else:
                subset = "full"
            counts[subset] += 1
            items.append({
                "id": f"dup-{row.row_id}",
                "subset": subset,
                "kind": "row",
                "text": row.question,
                "expected_row_id": row.row_id,
                "expected_intent": intent_dict(row),
            })
    assert len(items) == 417, f"expected 417 dup rows, collected {len(items)}"
    return items, counts


def select_plain_rows(engine: CatalogueEngine) -> list[dict]:
    """Representative non-duplicate rows across metric/request families."""
    preferences = [
        ("Best", "Final node", "All Frequencies"),
        ("Worst", "Final node", "All Frequencies"),
        ("Maximum", "Final node", "All Frequencies"),
        ("Minimum", "Final node", "All Frequencies"),
        ("Individual frequency final value", "Final node", "1500 MHz"),
        ("Individual frequency all nodes", "All nodes", "2100 MHz"),
        ("Part summary", "ADL8124", "All Frequencies"),
        ("Node summary", "2", "All Frequencies"),
        ("Complete history", "Final node", "All Frequencies"),
        ("Start-stop change", "Final node", "All Frequencies"),
        ("Ripple", "Final node", "All Frequencies"),
        ("Average and median", "Final node", "All Frequencies"),
        ("Power headroom", "Final node", "All Frequencies"),
        ("Largest stage change", "Final node", "All Frequencies"),
        ("Particular frequency", "Final node", "1800 MHz"),
    ]
    items = []
    metrics = sorted(engine.metrics())
    for metric in metrics:
        taken = 0
        for request, scope, freq in preferences:
            if taken >= 4:
                break
            # try the natural unit per metric family (dB for ratios, dBm powers...)
            for unit in ("dB", "dBm", "dB20", "deg", "Ohm"):
                key5 = (metric, request, scope, freq, unit)
                rows = engine._index.get(key5)
                if rows and len(rows) == 1:
                    row = rows[0]
                    items.append({
                        "id": f"plain-{row.row_id}",
                        "subset": "core",
                        "kind": "row",
                        "text": row.question,
                        "expected_row_id": row.row_id,
                        "expected_intent": intent_dict(row),
                    })
                    taken += 1
                    break
    return items


def build_paraphrases(engine: CatalogueEngine) -> list[dict]:
    """Curated paraphrases; each is resolved against the catalogue NOW so the
    expected row ID is catalogue-verified, not assumed.

    Targets are FULL intent keys (5 dims + params key) verified against the
    engine's intent index, which is proven duplicate-free.
    """
    # (paraphrase, 5-field key, params key)
    groups = [
        ("What is the worst Cascaded Gain in the final node?",
         ("CGAIN", "Worst", "Final node", "All Frequencies", "dB"), ("none",)),
        ("Give me the minimum cascade gain at the final node.",
         ("CGAIN", "Minimum", "Final node", "All Frequencies", "dB"), ("none",)),
        ("What's the minimum CGAIN for the final node?",
         ("CGAIN", "Minimum", "Final node", "All Frequencies", "dB"), ("none",)),
        ("How much stage power gain is at its best in the final node?",
         ("GAIN", "Best", "Final node", "All Frequencies", "dB"), ("none",)),
        ("Show me the best mismatch loss possible in the final node.",
         ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB"), ("none",)),
        ("Across all nodes, where does the mismatch loss go above 5 dB?",
         ("MismatchLoss", "Threshold Above", "All nodes", "All Frequencies", "dB"),
         ("threshold", 5.0)),
        ("Which spots have mismatch loss under 2 dB anywhere in the network?",
         ("MismatchLoss", "Threshold Below", "All nodes", "All Frequencies", "dB"),
         ("threshold", 2.0)),
        ("How much desired channel power is there at 1200 MHz in the final node?",
         ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"), ("none",)),
        ("How much total node power sits above the desired channel power at 1200 MHz?",
         ("TNP", "Individual frequency power difference", "Final node", "1200 MHz", "dBm"),
         ("refqual", "desired channel power")),
        ("How much total node power sits above the channel power at 1200 MHz?",
         ("TNP", "Individual frequency power difference", "Final node", "1200 MHz", "dBm"),
         ("refqual", "channel power")),
        ("Summarize the mismatch loss for part ADL8124.",
         ("MismatchLoss", "Part summary", "ADL8124", "All Frequencies", "dB"), ("none",)),
        ("Give me the node summary for mismatch loss at node 2.",
         ("MismatchLoss", "Node summary", "2", "All Frequencies", "dB"), ("none",)),
        ("What was the cascaded gain's final value at 1500 MHz?",
         ("CGAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
         ("none",)),
        ("Track the complete mismatch loss history everywhere.",
         ("MismatchLoss", "Complete history", "All frequencies, parts and nodes",
          "All Frequencies", "dB"), ("none",)),
        ("Where does cascaded compression get closest to 1 dB20 anywhere?",
         ("CCOMP", "Closest value", "All nodes", "All Frequencies", "dB20"),
         ("closest", 1.0)),
        ("How much total node power is above the channel noise power across frequency?",
         ("NDCP", "Power difference", "Final node", "All Frequencies", "dBm"),
         ("refqual", "channel noise power")),
    ]
    items = []
    for text, key5, params_key in groups:
        intent_key = key5 + tuple(params_key)
        rows = engine._intent_index.get(intent_key)
        if not rows or len(rows) != 1:
            raise SystemExit(
                f"Paraphrase target not uniquely resolvable in the intent "
                f"index: {intent_key} (rows={len(rows) if rows else 0})"
            )
        items.append({
            "id": f"para-{len(items)+1:02d}",
            "subset": "core",
            "kind": "paraphrase",
            "text": text,
            "expected_row_id": rows[0].row_id,
            "expected_intent": intent_dict(rows[0]),
        })
    return items


def build_clarifications() -> list[dict]:
    return [
        {"id": "clar-01", "kind": "clarification", "subset": "smoke",
         "text": "What is the gain at 2500 MHz?",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing": ["metric"]},
        {"id": "clar-02", "kind": "clarification", "subset": "smoke",
         "text": "Where is Mismatch Loss above the limit?",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing_contains": ["threshold"]},
        {"id": "clar-07", "kind": "clarification", "subset": "smoke",
         "text": "Give me the lowest cascade gain at the final node.",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing_contains": ["request"],
         "expect_hints_contain": ["Minimum", "Worst"],
         "note": "'lowest' is deliberately NOT mapped to a canonical REQUEST; "
                 "with frequency undetermined the candidates are presented "
                 "as hints rather than full intents"},
        {"id": "clar-08", "kind": "clarification", "subset": "smoke",
         "text": "What is the best mismatch loss in the ADL8124 family?",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing_contains": ["scope"],
         "note": "ADL8124 matches both ADL8124 and ADL8124_1 scopes"},
        {"id": "clar-03", "kind": "clarification", "subset": "core",
         "text": "What is the best mismatch loss?",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing": ["scope"]},
        {"id": "clar-04", "kind": "clarification", "subset": "core",
         "text": "Show the total node power difference at 1200 MHz.",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing_contains": ["reference qualifier"]},
        {"id": "clar-05", "kind": "clarification", "subset": "core",
         "text": "Where does the mismatch loss fall in the range?",
         "expect_status": "NEEDS_CLARIFICATION",
         "expect_missing_contains": ["range"]},
        {"id": "clar-06", "kind": "clarification", "subset": "core",
         "text": "What was it then?",
         "expect_status": "NEEDS_CLARIFICATION"},
    ]


def build_not_founds() -> list[dict]:
    return [
        {"id": "nf-01", "kind": "not_found", "subset": "smoke",
         "text": "What is the best CGAIN at 3400 MHz?",
         "expect_status": "NOT_FOUND",
         "expect_message_contains": ["3400 MHz"]},
        {"id": "nf-06", "kind": "not_found", "subset": "smoke",
         "text": "What is the best mismatch loss for part FOO123?",
         "expect_status": "NOT_FOUND",
         "expect_message_contains": ["FOO123"]},
        {"id": "nf-07", "kind": "not_found", "subset": "smoke",
         "text": "What is the best CGAIN in node 99?",
         "expect_status": "NOT_FOUND",
         "expect_message_contains": ["99"]},
        {"id": "nf-08", "kind": "not_found", "subset": "core",
         "text": "What is the return loss in the final node?",
         "expect_status": "NOT_FOUND",
         "note": "unknown metric; the LLM must report it via "
                 "unsupported_dimension, NOT via clarification"},
        {"id": "nf-02", "kind": "not_found", "subset": "core",
         "text": "What is the mismatch loss at 3500 MHz in the final node?",
         "expect_status": "NOT_FOUND",
         "expect_message_contains": ["3500 MHz"]},
        {"id": "nf-03", "kind": "not_found", "subset": "core",
         "text": "Where is Mismatch Loss above 0.2 dB in All nodes?",
         "expect_status": "NOT_FOUND"},
        {"id": "nf-05", "kind": "not_found", "subset": "core",
         "text": "Where is Cascaded Gain between 7 and 9 dB?",
         "expect_status": "NOT_FOUND"},
    ]


def build_unsupported() -> list[dict]:
    return [
        {"id": "uns-01", "kind": "unsupported", "subset": "smoke",
         "text": "Plot the cascaded gain over time for the final node.",
         "expect_status": "UNSUPPORTED",
         "note": "request type (plotting) is outside catalogue capabilities"},
        {"id": "uns-02", "kind": "unsupported", "subset": "core",
         "text": "Export the mismatch loss catalogue to CSV for me.",
         "expect_status": "UNSUPPORTED"},
        {"id": "uns-03", "kind": "unsupported", "subset": "core",
         "text": "Predict what the gain will be next week.",
         "expect_status": "UNSUPPORTED"},
    ]


def verify(engine: CatalogueEngine, items: list[dict]) -> None:
    """Re-verify every row/paraphrase expectation through the trusted engine."""
    for item in items:
        if item["kind"] not in ("row", "paraphrase"):
            continue
        exp = item["expected_intent"]
        key5 = (exp["metric"], exp["request"], exp["scope"],
                exp["frequency_selection"], exp["unit"])
        rows = engine._index.get(key5, [])
        matches = [r for r in rows if r.row_id == item["expected_row_id"]]
        if not matches:
            raise SystemExit(
                f"Gold item {item['id']} expectation unverifiable: {key5} "
                f"row_id={item['expected_row_id']}"
            )


def main() -> None:
    engine = CatalogueEngine()
    OUT_DIR.mkdir(exist_ok=True)

    dup_all, dup_counts = select_dup_rows(engine)
    plain = select_plain_rows(engine)
    rows = dup_all + plain
    verify(engine, rows)

    paraphrases = build_paraphrases(engine)
    verify(engine, paraphrases)

    clarifications = build_clarifications()
    not_founds = build_not_founds()
    unsupported = build_unsupported()

    def write(path: Path, items: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"wrote {len(items):4d} items -> {path.name}")

    write(OUT_DIR / "gold_set.jsonl", rows)
    write(OUT_DIR / "paraphrases.jsonl", paraphrases)
    write(OUT_DIR / "clarifications.jsonl",
          clarifications + not_founds + unsupported)

    print(f"\ndup rows: {len(dup_all)} (smoke={dup_counts['smoke']}, "
          f"core={dup_counts['core']}, full={dup_counts['full']})")
    print(f"totals: {len(rows)} row items, {len(paraphrases)} paraphrases, "
          f"{len(clarifications)} clarifications, {len(not_founds)} not_founds, "
          f"{len(unsupported)} unsupported")


if __name__ == "__main__":
    main()
