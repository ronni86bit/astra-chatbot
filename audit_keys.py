"""Lookup-key integrity audit for the canonical 5-field key.

Canonical lookup key:
    (metric, request, scope, frequency_selection, unit)

This audit reads the actual workbook directly with openpyxl — independently
of the engine — applies the engine's exact normalization and placeholder
rules, and objectively verifies whether the 5-field key is unique.

Writes the full duplicate disclosure to:
    audit/duplicate_keys_5field.txt

NO LLM, NO RAG, NO vector search. Pure deterministic structural analysis.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

WORKBOOK = r"C:\Ronni\Projects\astra chatbot\SystemVue_RF_Unique_Question_Assistant_V63.xlsx"
SHEET = "RF Questions and Answers"
DISCLOSURE_PATH = Path(r"C:\Ronni\Projects\astra chatbot\audit\duplicate_keys_5field.txt")

PLACEHOLDER_LABELS = {"METRIC", "REQUEST", "ANSWER TYPE", "SCOPE", "1"}


def norm(s) -> str:
    if s is None:
        return ""
    return str(s).strip()


def load_records():
    wb = openpyxl.load_workbook(WORKBOOK, data_only=True)
    ws = wb[SHEET]

    records, unindexable, placeholder, total_data_rows = [], 0, 0, 0

    for row_idx in range(5, ws.max_row + 1):
        cells = ws[row_idx]
        if len(cells) < 9:
            continue

        row_id, question, answer, metric, request, scope, unit, answer_type, freq = (
            cells[0].value, cells[1].value, cells[2].value, cells[3].value,
            cells[4].value, cells[5].value, cells[6].value, cells[7].value,
            cells[8].value,
        )

        # Empty tail row: not a record at all
        if row_id is None and question is None and answer is None:
            continue
        total_data_rows += 1

        # Malformed rows: any core value None -> unindexable
        if any(v is None for v in [row_id, question, answer, metric, request, scope]):
            unindexable += 1
            continue

        # Placeholder rows (engine default rules)
        if any(norm(v) in PLACEHOLDER_LABELS for v in [metric, request, answer_type, scope]):
            placeholder += 1
            continue

        records.append({
            "row_id": int(row_id),
            "question": norm(question),
            "answer": norm(answer),
            "metric": norm(metric),
            "request": norm(request),
            "scope": norm(scope),
            "unit": norm(unit) if unit else "",
            "answer_type": norm(answer_type),
            "frequency_selection": freq if freq else "",
        })

    return records, unindexable, placeholder, total_data_rows


def key5(r):
    """Canonical 5-field key: (metric, request, scope, frequency_selection, unit)."""
    return (r["metric"], r["request"], r["scope"],
            r["frequency_selection"], r["unit"])


def main() -> None:
    records, unindexable, placeholder, total_data_rows = load_records()

    index = defaultdict(list)
    for r in records:
        index[key5(r)].append(r)

    unique_keys = len(index)
    dup_keys = {k: v for k, v in index.items() if len(v) > 1}

    # ---- Exact required report ------------------------------------------
    lines = []
    lines.append("=" * 72)
    lines.append("5-FIELD LOOKUP-KEY INTEGRITY AUDIT (independent, direct workbook read)")
    lines.append("Key: (metric, request, scope, frequency_selection, unit)")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"Total candidate data rows in sheet      : {total_data_rows}")
    lines.append(f"Total non-placeholder records           : {len(records)}")
    lines.append(f"Unique 5-field keys                     : {unique_keys}")
    lines.append(f"Duplicate 5-field keys (keys with >1row): {len(dup_keys)}")
    lines.append(f"Rows covered by duplicate keys          : {sum(len(v) for v in dup_keys.values())}")
    lines.append(f"Unindexable records (malformed/None)    : {unindexable}")
    lines.append(f"Placeholder records excluded            : {placeholder}")
    lines.append("")

    ids = Counter(r["row_id"] for r in records)
    id_dups = {i: c for i, c in ids.items() if c > 1}
    lines.append(f"Unique row IDs                          : {len(ids)}  (duplicate IDs: {len(id_dups)})")
    lines.append("")

    if not dup_keys:
        lines.append("5-FIELD KEY VERIFIED UNIQUE")
        report = "\n".join(lines)
        print(report)
        DISCLOSURE_PATH.write_text(report, encoding="utf-8")
        return

    lines.append("5-FIELD KEY IS **NOT** UNIQUE — full duplicate disclosure follows.")
    lines.append("Minimum additional dimension required: a 6th 'query parameter' field")
    lines.append("(threshold / closest / range bound; TNP reference qualifier) which the")
    lines.append("workbook stores ONLY inside the QUESTION text, not as a column.")
    lines.append("")

    # ---- Full duplicate disclosure ---------------------------------------
    lines.append("-" * 72)
    lines.append(f"DUPLICATE DISCLOSURE: {len(dup_keys)} duplicate 5-field keys")
    lines.append("-" * 72)
    for n, (k, rows) in enumerate(sorted(dup_keys.items()), 1):
        lines.append(f"\nDuplicate #{n}: key={k}")
        for r in rows:
            q = r["question"]
            a = r["answer"].replace("\n", " | ")
            lines.append(f"  ID={r['row_id']}")
            lines.append(f"    QUESTION    : {q}")
            lines.append(f"    ANSWER      : {a}")
            lines.append(f"    ANSWER TYPE : {r['answer_type']}")

    # ---- Discriminating-field analysis -----------------------------------
    lines.append("")
    lines.append("-" * 72)
    lines.append("DISCRIMINATING-FIELD ANALYSIS (what differs within each dup key)")
    lines.append("-" * 72)
    diff_counter = Counter()
    for k, rows in dup_keys.items():
        differing = []
        for field in ["answer_type", "question", "answer"]:
            if len({r[field] for r in rows}) > 1:
                differing.append(field)
        diff_counter[tuple(differing) if differing else ("nothing (exact content clones)",)] += 1
    for sig, cnt in diff_counter.most_common():
        lines.append(f"  {cnt} keys differ only in: {list(sig)}")

    clone_keys = sum(
        1 for rows in dup_keys.values()
        if len({r["question"] for r in rows}) != len(rows)
    )
    lines.append(f"  Keys where QUESTION text itself repeats (true clones): {clone_keys}")

    report = "\n".join(lines)
    print("\n".join(report.splitlines()[:14]))
    print(f"\n[... {len(dup_keys)} duplicate keys fully disclosed; see {DISCLOSURE_PATH.name} ...]")
    print()
    print("Duplicate key metric x request distribution:")
    mr = Counter((k[0], k[1]) for k in dup_keys)
    for (m, rq), c in sorted(mr.items()):
        print(f"  {m:12s} x {rq:40s} x {c} keys")

    DISCLOSURE_PATH.parent.mkdir(exist_ok=True)
    DISCLOSURE_PATH.write_text(report, encoding="utf-8-sig")
    print(f"\nFull disclosure written to: {DISCLOSURE_PATH}")


if __name__ == "__main__":
    main()
