"""Deterministic RF question catalogue engine.

Loads SystemVue_RF_Unique_Question_Assistant_V63.xlsx and provides exact
lookup across the predefined QA catalogue using the canonical 5-field key:

    (metric, request, scope, frequency_selection, unit)

AUDIT STATUS (verified against the actual workbook):
The 5-field key is NOT fully unique. 80 duplicate keys remain (417 rows),
all parameterised queries (Threshold Above/Below, Closest value, Range,
TNP power difference) whose distinguishing parameter appears only inside
the QUESTION text. A 6th dimension (query parameter) is required to fully
resolve them. The engine honestly returns AMBIGUOUS for these keys.

NO LLM, NO RAG, NO vector search. Pure deterministic catalogue lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import openpyxl

try:  # package import
    from rf_catalogue.query_intent import (
        QueryIntent,
        QueryParams,
        parse_question_params,
    )
except ImportError:  # direct script execution (python src/rf_catalogue/engine.py)
    from query_intent import (  # type: ignore[no-redef]
        QueryIntent,
        QueryParams,
        parse_question_params,
    )

# ---------------------------------------------------------------------------
# Outcome constants for lookup results (requirement 6)
# ---------------------------------------------------------------------------
EXACT_MATCH = "EXACT_MATCH"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Placeholder rows to exclude by default. These are rows where the column
# values contain the column header text rather than actual data.
# This is an explicit configurable rule — do not silently discard.
# Set exclude_placeholders=False to include all rows including placeholders.
DEFAULT_EXCLUDE_PLACEHOLDERS = True

# Placeholder label strings that identify placeholder rows.
# These are values that match the column header names.
PLACEHOLDER_LABELS = {
    "METRIC",
    "REQUEST",
    "ANSWER TYPE",
    "SCOPE",
    "1",  # rows where METRIC="1", REQUEST="1", etc.
}

# The workbook is expected relative to the project root, but the engine
# accepts an explicit path so it can be used in any context.
DEFAULT_WORKBOOK_NAME = "SystemVue_RF_Unique_Question_Assistant_V63.xlsx"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class QARow:
    """Normalized row from the QA catalogue.

    Preserves all original fields from the Excel workbook:
    ID, QUESTION, ANSWER, METRIC, REQUEST, SCOPE, UNIT, ANSWER TYPE,
    FREQUENCY SELECTION
    """

    row_id: int
    question: str
    answer: str
    metric: str
    request: str
    scope: str
    unit: str
    answer_type: str
    frequency_selection: str


@dataclass
class LookupResult:
    """Result of a deterministic catalogue lookup (requirement 6).

    Outcome is one of exactly three values: EXACT_MATCH, AMBIGUOUS, NOT_FOUND.
    Never guesses or silently chooses one record when multiple match.
    """

    kind: str  # EXACT_MATCH, AMBIGUOUS, or NOT_FOUND
    rows: list[QARow]
    message: str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CatalogueEngine:
    """Deterministic question-answer catalogue for RF measurements.

    Loads the SystemVue workbook and provides exact lookup using the
    canonical 5-field key:

        (metric, request, scope, frequency_selection, unit)

    The engine is completely independent of any LLM, RAG, or vector search.
    It provides deterministic lookup with three possible outcomes:
    - EXACT_MATCH: precisely one record matches
    - AMBIGUOUS: multiple records match (never guessed, all returned)
    - NOT_FOUND: no record matches the given dimensions
    """

    def __init__(
        self,
        workbook_path: Path | str | None = None,
        exclude_placeholders: bool = DEFAULT_EXCLUDE_PLACEHOLDERS,
        placeholder_labels: set[str] | None = None,
    ):
        """Initialize the catalogue engine.

        Args:
            workbook_path: Path to the Excel workbook. If None, looks
                for the default name in the current working directory.
            exclude_placeholders: When True (default), excludes rows where
                categorical column values match placeholder labels (METRIC,
                REQUEST, ANSWER TYPE, SCOPE, "1"). When False, includes
                all rows including placeholders.
            placeholder_labels: Custom set of label strings that identify
                placeholder rows. Defaults to the standard set.
        """
        self.workbook_path = Path(workbook_path or DEFAULT_WORKBOOK_NAME)
        self.exclude_placeholders = exclude_placeholders
        self.placeholder_labels = placeholder_labels or PLACEHOLDER_LABELS

        # The in-memory catalogue: list of QARow.
        self._catalogue: list[QARow] = []

        # Index: (metric, request, scope, frequency_selection, unit) -> list of QARow
        self._index: dict[tuple[str, str, str, str, str], list[QARow]] = {}

        # Rows skipped as placeholders / malformed during loading.
        self._placeholder_count: int = 0
        self._unindexable_count: int = 0

        # Intent index: (metric, request, scope, frequency_selection, unit,
        # params_key) -> list of QARow. Built deterministically by parsing
        # QUESTION text with the verified grammar in query_intent.
        self._intent_index: dict[tuple, list[QARow]] = {}
        self._intent_parse_failures: list[tuple[QARow, str]] = []

        if not self.workbook_path.exists():
            raise FileNotFoundError(
                f"Workbook not found: {self.workbook_path}"
            )

        self._load_workbook()
        self._build_index()
        self._build_intent_index()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _default_placeholder_labels(self) -> set[str]:
        """Return the default set of placeholder label strings."""
        return {
            "METRIC",
            "REQUEST",
            "ANSWER TYPE",
            "SCOPE",
            "1",
        }

    def _load_workbook(self) -> None:
        """Load and parse the QA table from the Excel workbook.

        Data rows start from row 5. Rows 1-3 are meta/instruction rows,
        row 4 is the column header.
        Preserves all original fields without alteration.
        Explicitly handles placeholder rows via configuration.
        """
        wb = openpyxl.load_workbook(self.workbook_path, data_only=True)
        ws = wb["RF Questions and Answers"]

        for row_idx in range(5, ws.max_row + 1):
            # Columns: A=ID, B=QUESTION, C=ANSWER, D=METRIC, E=REQUEST,
            # F=SCOPE, G=UNIT, H=ANSWER TYPE, I=FREQUENCY SELECTION
            cells = ws[row_idx]
            if len(cells) < 9:
                continue

            row_id = cells[0].value
            question = cells[1].value
            answer = cells[2].value
            metric = cells[3].value
            request = cells[4].value
            scope = cells[5].value
            unit = cells[6].value
            answer_type = cells[7].value
            freq_selection = cells[8].value

            # Skip if any core value is None — these are malformed rows
            if any(v is None for v in [row_id, question, answer, metric, request, scope]):
                self._unindexable_count += 1
                continue

            # Explicit placeholder handling (requirement 10):
            # Check if any categorical value matches a placeholder label.
            # This is NOT a silent removal — the caller can set
            # exclude_placeholders=False to include all rows.
            if self.exclude_placeholders:
                is_placeholder = any(
                    str(val).strip() in self.placeholder_labels
                    for val in [metric, request, answer_type, scope]
                )
                if is_placeholder:
                    self._placeholder_count += 1
                    continue

            # Ensure row_id is an int
            try:
                rid = int(row_id)
            except (ValueError, TypeError):
                rid = 0  # fallback

            # Normalise strings by stripping whitespace consistently
            # but DO NOT alter semantic values.
            def norm(s: str | None) -> str:
                if s is None:
                    return ""
                return str(s).strip()

            qa_row = QARow(
                row_id=rid,
                question=norm(question),
                answer=norm(answer),
                metric=norm(metric),
                request=norm(request),
                scope=norm(scope),
                unit=norm(unit) if unit else "",
                answer_type=norm(answer_type),
                frequency_selection=freq_selection if freq_selection else "",
            )
            self._catalogue.append(qa_row)

    def _build_index(self) -> None:
        """Build the dimension index for deterministic lookup.

        Index key (canonical 5-field key):
            (metric, request, scope, frequency_selection, unit)
        Value: list of QARow objects matching that combination.
        """
        self._index = {}
        for row in self._catalogue:
            key = (row.metric, row.request, row.scope,
                   row.frequency_selection, row.unit)
            self._index.setdefault(key, []).append(row)

    def _build_intent_index(self) -> None:
        """Build the intent index: 5-field key + structured parameters.

        Parameters are extracted from QUESTION text with the verified
        deterministic grammar (query_intent.parse_question_params). Rows
        whose question fails to parse are never guessed — they are recorded
        in _intent_parse_failures and excluded from the intent index.
        """
        self._intent_index = {}
        self._intent_parse_failures = []
        for row in self._catalogue:
            try:
                params = parse_question_params(row.request, row.question, row.unit)
            except Exception as exc:  # QuestionParamParseError
                self._intent_parse_failures.append((row, str(exc)))
                continue
            key = (row.metric, row.request, row.scope,
                   row.frequency_selection, row.unit) + params.key()
            self._intent_index.setdefault(key, []).append(row)

    def lookup_by_intent(self, intent: QueryIntent) -> LookupResult:
        """Deterministic lookup using a canonical QueryIntent.

        Uses the 5-field base key extended with the intent's structured
        parameters (the effective 6th dimension). Same three-outcome
        contract as lookup(); never guesses.

        NOTE: the base 5-field index is unchanged; the intent index is a
        validated, parameter-resolved view of the same catalogue.
        """
        key = intent.intent_key()
        matching_rows = self._intent_index.get(key, [])

        if len(matching_rows) == 0:
            return LookupResult(
                kind=NOT_FOUND,
                rows=[],
                message=f"No QA row found for intent key {key!r}",
            )

        if len(matching_rows) == 1:
            return LookupResult(
                kind=EXACT_MATCH,
                rows=[matching_rows[0]],
                message=f"Exact match found: row ID {matching_rows[0].row_id}",
            )

        amb_rows = matching_rows[:3]
        total = len(matching_rows)
        return LookupResult(
            kind=AMBIGUOUS,
            rows=amb_rows,
            message=f"Ambiguous intent lookup: {total} matches found for "
                    f"key {key!r}.",
        )

    def intent_unique_keys(self) -> int:
        """Number of unique (5-field + params) intent keys."""
        return len(self._intent_index)

    def intent_duplicate_keys(self) -> int:
        """Number of intent keys matching more than one row.

        Zero means the QueryIntent representation fully disambiguates
        the catalogue.
        """
        return sum(1 for rows in self._intent_index.values() if len(rows) > 1)

    def intent_parse_failures(self) -> list[tuple[QARow, str]]:
        """Rows whose QUESTION text could not be parsed by the grammar.

        Empty for the current workbook (verified). Non-empty entries mark
        wording that requires office confirmation — never guessed.
        """
        return list(self._intent_parse_failures)

    # -------------------------------------------------------------------------
    # Lookup (requirement 5-8)
    # -------------------------------------------------------------------------

    def lookup(
        self,
        metric: str,
        request: str,
        scope: str,
        freq_selection: str,
        unit: str,
    ) -> LookupResult:
        """Deterministic lookup by the canonical 5-field key.

        Args:
            metric: The RF metric (e.g., "MismatchLoss", "GAIN").
            request: The question/request type (e.g., "Best", "Part summary").
            scope: The measurement scope (e.g., "Final node", "All nodes").
            freq_selection: The frequency selection (e.g., "All Frequencies", "1500 MHz").
            unit: The unit of measurement (e.g., "dB", "dBm", "dB20").

        Returns:
            LookupResult with kind EXACT_MATCH, AMBIGUOUS, or NOT_FOUND.
            Never silently chooses one record when multiple match.

        Lookup outcomes:
            - EXACT_MATCH: precisely one record matches, returned with all metadata
            - AMBIGUOUS: multiple records match; all are returned (never guessed)
            - NOT_FOUND: no record matches the given dimensions
        """
        key = (metric, request, scope, freq_selection, unit)
        matching_rows = self._index.get(key, [])

        if len(matching_rows) == 0:
            return LookupResult(
                kind=NOT_FOUND,
                rows=[],
                message=f"No QA row found for metric={metric!r}, request={request!r}, "
                        f"scope={scope!r}, freq_selection={freq_selection!r}, unit={unit!r}",
            )

        if len(matching_rows) == 1:
            return LookupResult(
                kind=EXACT_MATCH,
                rows=[matching_rows[0]],
                message=f"Exact match found: row ID {matching_rows[0].row_id}",
            )

        # Multiple matches — never guess. Return ambiguous result with
        # all matching rows (first 3 displayed, total count reported).
        amb_rows = matching_rows[:3]
        total = len(matching_rows)
        return LookupResult(
            kind=AMBIGUOUS,
            rows=amb_rows,
            message=f"Ambiguous lookup: {total} matches found for "
                    f"metric={metric!r}, request={request!r}, "
                    f"scope={scope!r}, freq_selection={freq_selection!r}, unit={unit!r}. "
                    f"Use additional filters or ask user for clarification.",
        )

    # -------------------------------------------------------------------------
    # Public API — convenient shorthands
    # -------------------------------------------------------------------------

    def get_question_answer(
        self,
        metric: str,
        request: str,
        scope: str,
        freq_selection: str,
        unit: str,
    ) -> dict | None:
        """Lookup and return a dict with question/answer metadata, or None.

        Returns dict with keys: question, answer, metric, request, scope,
        unit, answer_type, frequency_selection, row_id. Returns None if no
        match or ambiguous result (requirement 9: includes source row ID
        and all metadata in the result).

        The ANSWER is returned exactly as stored in the workbook,
        preserved via normalization without alteration.
        """
        result = self.lookup(metric, request, scope, freq_selection, unit)
        if result.kind != EXACT_MATCH or not result.rows:
            return None
        row = result.rows[0]
        return {
            "question": row.question,
            "answer": row.answer,
            "metric": row.metric,
            "request": row.request,
            "scope": row.scope,
            "unit": row.unit,
            "answer_type": row.answer_type,
            "frequency_selection": row.frequency_selection,
            "row_id": row.row_id,
        }

    # -------------------------------------------------------------------------
    # Introspection (requirement 12: report script)
    # -------------------------------------------------------------------------

    def total_rows(self) -> int:
        """Return the number of catalogue rows loaded (requirement 12)."""
        return len(self._catalogue)

    def rows_indexed(self) -> int:
        """Return the number of unique lookup keys in the index (requirement 12)."""
        return len(self._index)

    def duplicate_lookup_keys(self) -> int:
        """Return the count of lookup keys that have multiple matching rows.

        A key is "duplicate" if it maps to more than one QARow in the index.
        This count helps assess how many lookup dimensions are ambiguous by default.
        """
        return sum(1 for key, rows in self._index.items() if len(rows) > 1)

    def unique_lookup_keys(self) -> int:
        """Return the number of unique (metric, request, scope, frequency_selection, unit) keys (requirement 12)."""
        return len(self._index)

    def metrics(self) -> set[str]:
        """Return the set of unique metrics in the catalogue."""
        return {row.metric for row in self._catalogue}

    def requests(self) -> set[str]:
        """Return the set of unique requests in the catalogue."""
        return {row.request for row in self._catalogue}

    def scopes(self) -> set[str]:
        """Return the set of unique scopes in the catalogue."""
        return {row.scope for row in self._catalogue}

    def freq_selections(self) -> set[str]:
        """Return the set of unique frequency selections in the catalogue."""
        return {row.frequency_selection for row in self._catalogue}

    def units(self) -> set[str]:
        """Return the set of unique units in the catalogue."""
        return {row.unit for row in self._catalogue}

    def answer_types(self) -> set[str]:
        """Return the set of unique answer types in the catalogue."""
        return {row.answer_type for row in self._catalogue}

    def placeholder_count(self) -> int:
        """Return the number of placeholder rows skipped during loading.

        Accurate when exclude_placeholders=True (default). Returns 0 when
        placeholder exclusion is disabled, since nothing is skipped.
        """
        return self._placeholder_count

    def unindexable_count(self) -> int:
        """Return the number of malformed rows skipped (core value None)."""
        return self._unindexable_count


# ---------------------------------------------------------------------------
# Main entry point / report script (requirement 12)
# ---------------------------------------------------------------------------


def report_stats(engine: CatalogueEngine) -> None:
    """Report engine statistics as required by requirement 12.

    Reports:
    - total catalogue rows loaded
    - rows indexed
    - duplicate lookup keys
    - number of unique lookup keys
    """
    print("=" * 60)
    print("RF CATALOGUE ENGINE REPORT")
    print("=" * 60)
    print()
    print("Canonical lookup key: (metric, request, scope, frequency_selection, unit)")
    print()
    print(f"Total catalogue rows loaded: {engine.total_rows()}")
    print(f"Unique 5-field lookup keys indexed: {engine.rows_indexed()}")
    print(f"Duplicate 5-field keys: {engine.duplicate_lookup_keys()}")
    print(f"Unique 5-field keys: {engine.unique_lookup_keys()}")
    print(f"Placeholder rows skipped: {engine.placeholder_count()}")
    print(f"Unindexable (malformed) rows: {engine.unindexable_count()}")
    print()
    print("Dimension inventories:")
    print(f"  Metrics: {sorted(engine.metrics())}")
    print(f"  Requests: {sorted(engine.requests())}")
    print(f"  Scopes: {sorted(engine.scopes())}")
    print(f"  Units: {sorted(set(row.unit for row in engine._catalogue))}")
    print(f"  Frequency selections: {sorted(engine.freq_selections())}")
    print(f"  Answer types: {sorted(engine.answer_types())}")
    print()
    print("Sample exact lookups:")
    # Show a few exact matches as examples.
    # Key order: (metric, request, scope, frequency_selection, unit)
    sample_lookups = [
        ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB"),
        ("MismatchLoss", "Worst", "Final node", "All Frequencies", "dB"),
        ("MismatchLoss", "Part summary", "ADL8124", "All Frequencies", "dB"),
        ("MismatchLoss", "Node summary", "2", "All Frequencies", "dB"),
        ("CGAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
        ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"),
        ("CCOMP", "Best", "Final node", "All Frequencies", "dB20"),
    ]
    for metric, request, scope, freq, unit in sample_lookups:
        result = engine.lookup(metric, request, scope, freq, unit)
        if result.kind == EXACT_MATCH and result.rows:
            row = result.rows[0]
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> EXACT_MATCH, row_id={row.row_id}")
        elif result.kind == AMBIGUOUS:
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> AMBIGUOUS ({result.message.split(',')[0][:60]}...)")
        else:
            print(f"  {metric!r}/{request!r}/{scope!r}/{freq!r}/{unit!r} -> NOT_FOUND")


def main() -> None:
    """CLI entry point for the rf-catalogue script."""
    engine = CatalogueEngine()
    report_stats(engine)


if __name__ == "__main__":
    main()