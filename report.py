"""Report script for the RF catalogue engine (requirement 12).

Canonical lookup key:
    (metric, request, scope, frequency_selection, unit)

Reports:
- total catalogue rows loaded
- unique 5-field keys indexed
- duplicate 5-field keys
- placeholder / unindexable rows
- dimension inventories
- sample lookups across all three outcomes
"""

import sys

sys.path.insert(0, r"C:\Ronni\Projects\astra chatbot\src")

from rf_catalogue.engine import CatalogueEngine, EXACT_MATCH, AMBIGUOUS, NOT_FOUND


def main():
    engine = CatalogueEngine()

    print("=" * 60)
    print("RF CATALOGUE ENGINE REPORT")
    print("=" * 60)
    print()
    print("Canonical lookup key: (metric, request, scope, frequency_selection, unit)")
    print()
    print(f"Total catalogue rows loaded: {engine.total_rows()}")
    print(f"Unique 5-field keys indexed: {engine.rows_indexed()}")
    print(f"Duplicate 5-field keys: {engine.duplicate_lookup_keys()}")
    print(f"Unique 5-field keys: {engine.unique_lookup_keys()}")
    print(f"Placeholder rows skipped: {engine.placeholder_count()}")
    print(f"Unindexable (malformed) rows: {engine.unindexable_count()}")
    print()
    print("Dimension inventories:")
    print(f"  Metrics: {sorted(engine.metrics())}")
    print(f"  Requests: {sorted(engine.requests())}")
    print(f"  Scopes: {sorted(engine.scopes())}")
    print(f"  Frequency selections: {sorted(engine.freq_selections())}")
    print(f"  Answer types: {sorted(engine.answer_types())}")
    print(f"  Units: {sorted(set(row.unit for row in engine._catalogue))}")
    print()
    print("Sample lookups (key order: metric, request, scope, frequency_selection, unit):")
    sample_lookups = [
        ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB"),
        ("MismatchLoss", "Worst", "Final node", "All Frequencies", "dB"),
        ("MismatchLoss", "Part summary", "ADL8124", "All Frequencies", "dB"),
        ("MismatchLoss", "Node summary", "2", "All Frequencies", "dB"),
        ("MismatchLoss", "Threshold Above", "All nodes", "All Frequencies", "dB"),
        ("CGAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
        ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"),
        ("CCOMP", "Best", "Final node", "All Frequencies", "dB20"),
        ("TNP", "Individual frequency power difference", "Final node", "1200 MHz", "dBm"),
    ]
    for metric, request, scope, freq, unit in sample_lookups:
        result = engine.lookup(metric, request, scope, freq, unit)
        if result.kind == EXACT_MATCH and result.rows:
            row = result.rows[0]
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> EXACT_MATCH, row_id={row.row_id}")
        elif result.kind == AMBIGUOUS:
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> AMBIGUOUS ({result.message.split(':')[0]})")
        else:
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> NOT_FOUND")


if __name__ == "__main__":
    main()
