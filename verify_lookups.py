"""Representative lookup verification across all key dimension classes.

Covers (requirement 8):
- metrics: CGAIN, GAIN, MismatchLoss, DCP (+ TNP, CCOMP for unit coverage)
- units: dB, dBm, dB20
- frequency selections: exact frequencies, All Frequencies
- scopes: part scopes, node scopes
- request classes: threshold queries, Best/Worst, summaries

Run:  python verify_lookups.py
"""

import sys

sys.path.insert(0, r"C:\Ronni\Projects\astra chatbot\src")

from rf_catalogue.engine import CatalogueEngine, EXACT_MATCH, AMBIGUOUS, NOT_FOUND

# Key order: (metric, request, scope, frequency_selection, unit)
BATTERY = [
    # --- metric coverage ---
    ("CGAIN", "Best", "Final node", "All Frequencies", "dB"),
    ("GAIN", "Best", "Final node", "All Frequencies", "dB"),
    ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB"),
    ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"),
    # --- unit coverage ---
    ("CCOMP", "Best", "Final node", "All Frequencies", "dB20"),
    ("TNP", "Best", "Final node", "All Frequencies", "dBm"),
    ("MismatchLoss", "Worst", "Final node", "All Frequencies", "dB"),
    # --- exact frequencies ---
    ("CGAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
    ("GAIN", "Individual frequency final value", "Final node", "1200 MHz", "dB"),
    ("DCP", "Particular frequency", "Final node", "2100 MHz", "dBm"),
    # --- All Frequencies ---
    ("MismatchLoss", "Maximum", "Final node", "All Frequencies", "dB"),
    ("MismatchLoss", "Minimum", "Final node", "All Frequencies", "dB"),
    # --- part scopes ---
    ("MismatchLoss", "Part summary", "ADL8124", "All Frequencies", "dB"),
    ("GAIN", "Part summary", "ADL8124", "All Frequencies", "dB"),
    # --- node scopes ---
    ("MismatchLoss", "Node summary", "2", "All Frequencies", "dB"),
    ("GAIN", "Node summary", "2", "All Frequencies", "dB"),
    # --- threshold queries (known-ambiguous class under the 5-field key) ---
    ("MismatchLoss", "Threshold Above", "All nodes", "All Frequencies", "dB"),
    ("MismatchLoss", "Threshold Below", "All nodes", "All Frequencies", "dB"),
    ("CCOMP", "Threshold Above", "All nodes", "All Frequencies", "dB20"),
    ("DCP", "Threshold Above", "All nodes", "All Frequencies", "dBm"),
    # --- known duplicate family: TNP power difference ---
    ("TNP", "Individual frequency power difference", "Final node", "1200 MHz", "dBm"),
    ("TNP", "Power difference", "Final node", "All Frequencies", "dBm"),
]


def main() -> None:
    engine = CatalogueEngine()
    print("=" * 78)
    print("REPRESENTATIVE LOOKUP VERIFICATION")
    print("Key: (metric, request, scope, frequency_selection, unit)")
    print("=" * 78)

    counts = {EXACT_MATCH: 0, AMBIGUOUS: 0, NOT_FOUND: 0}
    for key in BATTERY:
        result = engine.lookup(*key)
        counts[result.kind] += 1
        if result.kind == EXACT_MATCH:
            row = result.rows[0]
            print(f"EXACT     {key} -> ID {row.row_id} | AT={row.answer_type}")
        elif result.kind == AMBIGUOUS:
            ids = [r.row_id for r in result.rows]
            print(f"AMBIGUOUS {key} -> sample IDs {ids} (see audit disclosure)")
        else:
            print(f"NOT_FOUND {key}")

    print("-" * 78)
    print(f"Totals: {counts[EXACT_MATCH]} exact, {counts[AMBIGUOUS]} ambiguous, "
          f"{counts[NOT_FOUND]} not-found  (of {len(BATTERY)} probes)")
    print()
    print("Expected: exact for all non-parameterised keys; AMBIGUOUS only for the")
    print("threshold / power-difference classes documented in the audit.")


if __name__ == "__main__":
    main()
