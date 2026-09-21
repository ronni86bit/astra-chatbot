"""Summary of lookup-key integrity audit."""

import sys
sys.path.insert(0, r"C:\Ronni\Projects\astra chatbot\src")

from rf_catalogue.engine import CatalogueEngine

engine = CatalogueEngine()

total = engine.total_rows()
unique = engine.unique_lookup_keys()
dup = engine.duplicate_lookup_keys()

print(f"Total non-placeholder records: {total}")
print(f"Unique (metric, request, scope, freq_selection) combinations: {unique}")
print(f"Duplicate combinations: {dup}")
print()

# The total possible combinations minus unique minus duplicates should account for
# the rows that share keys
print(f"Key reconciliation: {total} rows across {unique} unique keys with {dup} duplicate keys")
print(f"Average rows per unique key: {total / unique:.2f}")
print(f"Average rows per duplicate key: {total / dup:.2f}")

# Check if the 4-field key is sufficient
# If every unique key maps to exactly 1 row, the key is sufficient
# If some keys map to >1 row, we need an additional dimension
print()
print("Key sufficiency analysis:")
print(f"  Keys with exactly 1 row: {sum(1 for k, v in engine._index.items() if len(v) == 1)}")
print(f"  Keys with more than 1 row: {sum(1 for k, v in engine._index.items() if len(v) > 1)}")
print(f"  Keys with 0 rows (should not happen): {sum(1 for k, v in engine._index.items() if len(v) == 0)}")

# Check specific dimensions that vary within duplicates
print()
print("Within duplicates, varying fields:")
for key, rows in engine._index.items():
    if len(rows) > 1:
        units = set(r.unit for r in rows)
        answer_types = set(r.answer_type for r in rows)
        # Check if questions differ
        questions = set(r.question[:30] for r in rows)
        print(f"  Key {key}: {len(rows)} rows, units={units}, answer_types={answer_types}, question_prefixes_differ={len(questions) > 1}")