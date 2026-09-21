"""Tests for the canonical QueryIntent model (structured query representation).

Covers:
- strict validation rules (request-aware params, numerics, enums, units)
- the deterministic QUESTION-text grammar derived from the workbook
- end-to-end disambiguation of EVERY duplicate family found in the audit
- the four required distinction proofs (threshold, closest, range, refqual)

Grounding: audit_keys.py + audit/duplicate_keys_5field.txt (80 dup keys,
417 rows) and the grammar analysis (497/497 family rows parse).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rf_catalogue.engine import EXACT_MATCH, NOT_FOUND
from rf_catalogue.query_intent import (
    CATALOGUE_UNITS,
    KNOWN_REQUESTS,
    OFFICE_REVIEW_ITEMS,
    ClosestTargetParams,
    NoParams,
    QueryIntent,
    QuestionParamParseError,
    RangeParams,
    ReferenceQualifier,
    ReferenceQualifierParams,
    ThresholdParams,
    intent_from_row,
    parse_question_params,
)

# Engine fixture is session-scoped in conftest.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_intent(request="Threshold Above", metric="MismatchLoss", scope="All nodes",
                frequency_selection="All Frequencies", unit="dB", params=None):
    return QueryIntent(
        metric=metric, request=request, scope=scope,
        frequency_selection=frequency_selection, unit=unit, params=params,
    )


# ---------------------------------------------------------------------------
# A. Strict validation rules
# ---------------------------------------------------------------------------


class TestValidationRules:
    def test_valid_threshold_intent(self):
        intent = make_intent(params=ThresholdParams(threshold=0.1))
        assert intent.params.threshold == 0.1

    def test_unit_must_be_catalogue_unit(self):
        with pytest.raises(ValidationError, match="not a catalogue unit"):
            make_intent(unit="dBX", params=ThresholdParams(threshold=1))

    def test_unit_is_case_sensitive(self):
        with pytest.raises(ValidationError):
            make_intent(unit="DB", params=ThresholdParams(threshold=1))

    def test_request_must_be_known(self):
        with pytest.raises(ValidationError, match="not a known catalogue request"):
            QueryIntent(metric="X", request="MegaQuery", scope="s",
                        frequency_selection="All Frequencies", unit="dB")

    # -- request-aware parameter enforcement --------------------------------

    def test_threshold_above_requires_threshold(self):
        with pytest.raises(ValidationError, match="requires ThresholdParams"):
            make_intent(request="Threshold Above")

    def test_threshold_below_requires_threshold(self):
        with pytest.raises(ValidationError, match="requires ThresholdParams"):
            make_intent(request="Threshold Below")

    def test_closest_value_requires_closest_target(self):
        with pytest.raises(ValidationError, match="requires ClosestTargetParams"):
            make_intent(request="Closest value")

    def test_range_requires_range_bounds(self):
        with pytest.raises(ValidationError, match="requires RangeParams"):
            make_intent(request="Range")

    def test_power_difference_requires_reference_qualifier(self):
        with pytest.raises(ValidationError, match="requires ReferenceQualifierParams"):
            make_intent(request="Power difference", unit="dBm")

    def test_ifpd_requires_reference_qualifier(self):
        with pytest.raises(ValidationError, match="requires ReferenceQualifierParams"):
            make_intent(request="Individual frequency power difference",
                        scope="Final node", frequency_selection="1200 MHz", unit="dBm")

    # -- irrelevant parameters rejected --------------------------------------

    @pytest.mark.parametrize("request_type", ["Best", "Worst", "Maximum", "Minimum",
                                              "Part summary", "Node summary",
                                              "Particular frequency",
                                              "Individual frequency final value",
                                              "All frequencies", "Complete history"])
    def test_params_rejected_on_non_family_requests(self, request_type):
        with pytest.raises(ValidationError, match="takes no parameters"):
            make_intent(request=request_type, params=ThresholdParams(threshold=1))

    def test_range_params_rejected_on_threshold_request(self):
        with pytest.raises(ValidationError, match="requires ThresholdParams"):
            make_intent(params=RangeParams(range_min=0, range_max=1))

    def test_closest_params_rejected_on_threshold_request(self):
        with pytest.raises(ValidationError, match="requires ThresholdParams"):
            make_intent(params=ClosestTargetParams(closest_target=1))

    def test_threshold_params_rejected_on_closest_request(self):
        with pytest.raises(ValidationError, match="requires ClosestTargetParams"):
            make_intent(request="Closest value", params=ThresholdParams(threshold=1))

    def test_threshold_params_rejected_on_range_request(self):
        with pytest.raises(ValidationError, match="requires RangeParams"):
            make_intent(request="Range", params=ThresholdParams(threshold=1))

    def test_threshold_params_rejected_on_power_difference(self):
        with pytest.raises(ValidationError, match="requires ReferenceQualifierParams"):
            make_intent(request="Power difference", unit="dBm",
                        params=ThresholdParams(threshold=1))

    # -- strict numerics ------------------------------------------------------

    def test_string_threshold_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(params=ThresholdParams(threshold="0.1"))

    def test_bool_threshold_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(params=ThresholdParams(threshold=True))

    def test_int_threshold_coerced_to_float(self):
        intent = make_intent(params=ThresholdParams(threshold=10))
        assert intent.params.threshold == 10.0
        assert isinstance(intent.params.threshold, float)

    def test_string_range_bound_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(request="Range", params=RangeParams(range_min="0.1", range_max=1))

    def test_bool_closest_target_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(request="Closest value",
                        params=ClosestTargetParams(closest_target=False))

    # -- range bound sanity ----------------------------------------------------

    def test_range_min_must_be_below_max(self):
        with pytest.raises(ValidationError, match="strictly less"):
            make_intent(request="Range", params=RangeParams(range_min=2, range_max=1))

    def test_range_min_equal_max_rejected(self):
        with pytest.raises(ValidationError, match="strictly less"):
            make_intent(request="Range", params=RangeParams(range_min=1, range_max=1))

    # -- controlled reference qualifiers ----------------------------------------

    def test_qualifier_enum_accepts_observed_values(self):
        for value in ("channel power", "desired channel power",
                      "channel noise power", "output-to-input"):
            intent = make_intent(request="Power difference", unit="dBm",
                                 params=ReferenceQualifierParams(
                                     reference_qualifier=ReferenceQualifier(value)))
            assert intent.params.reference_qualifier.value == value

    def test_qualifier_free_string_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(request="Power difference", unit="dBm",
                        params=ReferenceQualifierParams(reference_qualifier="some power"))

    def test_qualifier_none_rejected_on_family_request(self):
        with pytest.raises(ValidationError, match="concrete qualifier"):
            make_intent(request="Power difference", unit="dBm",
                        params=ReferenceQualifierParams(
                            reference_qualifier=ReferenceQualifier.NONE))

    # -- model hygiene ------------------------------------------------------------

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            make_intent(params=ThresholdParams(threshold=1, direction="above"))

    def test_intent_is_frozen(self):
        intent = make_intent(params=ThresholdParams(threshold=1))
        with pytest.raises(ValidationError):
            intent.metric = "GAIN"

    def test_empty_base_dimensions_rejected(self):
        with pytest.raises(ValidationError):
            make_intent(metric="", params=ThresholdParams(threshold=1))

    def test_intent_key_determinism(self):
        a = make_intent(params=ThresholdParams(threshold=0.1))
        b = make_intent(params=ThresholdParams(threshold=0.1))
        assert a.intent_key() == b.intent_key()
        assert a.intent_key() != make_intent(params=ThresholdParams(threshold=0.5)).intent_key()

    def test_catalogue_units_match_engine(self, engine):
        assert set(CATALOGUE_UNITS) == {r.unit for r in engine._catalogue}

    def test_known_requests_match_engine(self, engine):
        assert set(KNOWN_REQUESTS) == engine.requests()


# ---------------------------------------------------------------------------
# B. Deterministic QUESTION-text grammar (workbook-derived)
# ---------------------------------------------------------------------------


class TestQuestionGrammar:
    def test_threshold_above_parse(self):
        p = parse_question_params(
            "Threshold Above", "Where is Mismatch Loss above 0.1 dB?", "dB")
        assert p == ThresholdParams(threshold=0.1)

    def test_threshold_negative_value(self):
        p = parse_question_params(
            "Threshold Below", "Where is Desired Channel Power below -40 dBm?", "dBm")
        assert p == ThresholdParams(threshold=-40.0)

    def test_threshold_direction_mismatch_rejected(self):
        with pytest.raises(QuestionParamParseError, match="contradicts request"):
            parse_question_params(
                "Threshold Below", "Where is Mismatch Loss above 1 dB?", "dB")

    def test_closest_parse(self):
        p = parse_question_params(
            "Closest value", "Where is Cascaded Compression closest to 0.5 dB20?", "dB20")
        assert p == ClosestTargetParams(closest_target=0.5)

    def test_range_parse(self):
        p = parse_question_params(
            "Range", "Where is Cascaded Compression between 0.1 and 0.5 dB20?", "dB20")
        assert p == RangeParams(range_min=0.1, range_max=0.5)

    def test_range_non_increasing_bounds_rejected(self):
        with pytest.raises(QuestionParamParseError, match="not increasing"):
            parse_question_params(
                "Range", "Where is X between 2 and 1 dB?", "dB")

    def test_unit_cross_check_mismatch_rejected(self):
        with pytest.raises(QuestionParamParseError, match="question unit"):
            parse_question_params(
                "Threshold Above", "Where is X above 1 dBm?", "dB")

    def test_tnp_desired_qualifier(self):
        p = parse_question_params(
            "Individual frequency power difference",
            "Show total node power above desired channel power at individual frequency 1200 MHz?",
            "dBm")
        assert p.reference_qualifier is ReferenceQualifier.DESIRED_CHANNEL_POWER

    def test_tnp_plain_qualifier(self):
        p = parse_question_params(
            "Individual frequency power difference",
            "Show total node power above channel power at individual frequency 1200 MHz?",
            "dBm")
        assert p.reference_qualifier is ReferenceQualifier.CHANNEL_POWER

    def test_cndr_noise_qualifier(self):
        p = parse_question_params(
            "Power difference",
            "Show noise plus distortion power above channel noise power across frequency?",
            "dBm")
        assert p.reference_qualifier is ReferenceQualifier.CHANNEL_NOISE_POWER

    def test_cp_subtraction_qualifier(self):
        p = parse_question_params(
            "Power difference",
            "Show channel power minus desired channel power across frequency?",
            "dBm")
        assert p.reference_qualifier is ReferenceQualifier.DESIRED_CHANNEL_POWER

    def test_op1db_output_to_input_qualifier(self):
        p = parse_question_params(
            "Power difference",
            "Show output-to-input P1dB difference across frequency?",
            "dBm")
        assert p.reference_qualifier is ReferenceQualifier.OUTPUT_TO_INPUT

    def test_unknown_qualifier_wording_rejected_verbatim(self):
        with pytest.raises(QuestionParamParseError) as exc:
            parse_question_params(
                "Power difference",
                "Show total node power above mysterious reference across frequency?",
                "dBm")
        assert "mysterious reference" in str(exc.value)

    def test_non_family_request_returns_no_params(self):
        p = parse_question_params("Best", "What is the best Mismatch Loss?", "dB")
        assert isinstance(p, NoParams)


# ---------------------------------------------------------------------------
# C. Engine integration: the intent index fully disambiguates the catalogue
# ---------------------------------------------------------------------------


class TestIntentIndex:
    def test_intent_index_has_no_duplicates(self, engine):
        assert engine.intent_duplicate_keys() == 0

    def test_intent_index_covers_every_record(self, engine):
        assert engine.intent_unique_keys() == engine.total_rows() == 4768

    def test_no_parse_failures_in_current_workbook(self, engine):
        failures = engine.intent_parse_failures()
        assert failures == [], (
            f"{len(failures)} QUESTION texts failed the grammar; they require "
            f"office confirmation, e.g.: {failures[:3]}"
        )

    def test_five_field_index_still_has_known_duplicates(self, engine):
        """The base 5-field key is untouched: still the audited 80 dup keys."""
        assert engine.duplicate_lookup_keys() == 80


# ---------------------------------------------------------------------------
# D. Required distinction proofs (requirement 10)
# ---------------------------------------------------------------------------


class TestDistinctionProofs:
    def test_threshold_01_db_vs_10_db(self, engine):
        base = dict(metric="MismatchLoss", request="Threshold Above",
                    scope="All nodes", frequency_selection="All Frequencies", unit="dB")
        low = engine.lookup_by_intent(QueryIntent(**base, params=ThresholdParams(threshold=0.1)))
        high = engine.lookup_by_intent(QueryIntent(**base, params=ThresholdParams(threshold=10)))
        assert low.kind == high.kind == EXACT_MATCH
        assert low.rows[0].row_id == 2850
        assert high.rows[0].row_id == 2868
        assert low.rows[0] is not high.rows[0]

    def test_different_closest_targets(self, engine):
        base = dict(metric="CCOMP", request="Closest value", scope="All nodes",
                    frequency_selection="All Frequencies", unit="dB20")
        t01 = engine.lookup_by_intent(QueryIntent(**base, params=ClosestTargetParams(closest_target=0.1)))
        t05 = engine.lookup_by_intent(QueryIntent(**base, params=ClosestTargetParams(closest_target=0.5)))
        t1 = engine.lookup_by_intent(QueryIntent(**base, params=ClosestTargetParams(closest_target=1)))
        assert t01.rows[0].row_id == 3071
        assert t05.rows[0].row_id == 3074
        assert t1.rows[0].row_id == 3077
        assert len({t01.rows[0].row_id, t05.rows[0].row_id, t1.rows[0].row_id}) == 3

    def test_different_range_bounds(self, engine):
        base = dict(metric="CCOMP", request="Range", scope="All nodes",
                    frequency_selection="All Frequencies", unit="dB20")
        r1 = engine.lookup_by_intent(QueryIntent(**base, params=RangeParams(range_min=0.1, range_max=0.5)))
        r2 = engine.lookup_by_intent(QueryIntent(**base, params=RangeParams(range_min=0.5, range_max=1)))
        r3 = engine.lookup_by_intent(QueryIntent(**base, params=RangeParams(range_min=2, range_max=3)))
        assert r1.rows[0].row_id == 3084
        assert r2.rows[0].row_id == 3085
        assert r3.rows[0].row_id == 3087

    def test_desired_vs_plain_channel_power(self, engine):
        base = dict(metric="TNP", request="Individual frequency power difference",
                    scope="Final node", frequency_selection="1200 MHz", unit="dBm")
        desired = engine.lookup_by_intent(QueryIntent(
            **base, params=ReferenceQualifierParams(
                reference_qualifier=ReferenceQualifier.DESIRED_CHANNEL_POWER)))
        plain = engine.lookup_by_intent(QueryIntent(
            **base, params=ReferenceQualifierParams(
                reference_qualifier=ReferenceQualifier.CHANNEL_POWER)))
        assert desired.kind == plain.kind == EXACT_MATCH
        assert desired.rows[0].row_id == 2421
        assert plain.rows[0].row_id == 2423
        assert "desired channel power" in desired.rows[0].question
        assert "desired" not in plain.rows[0].question

    def test_desired_vs_plain_all_frequencies(self, engine):
        base = dict(metric="TNP", request="Power difference",
                    scope="Final node", frequency_selection="All Frequencies", unit="dBm")
        desired = engine.lookup_by_intent(QueryIntent(
            **base, params=ReferenceQualifierParams(
                reference_qualifier=ReferenceQualifier.DESIRED_CHANNEL_POWER)))
        plain = engine.lookup_by_intent(QueryIntent(
            **base, params=ReferenceQualifierParams(
                reference_qualifier=ReferenceQualifier.CHANNEL_POWER)))
        assert desired.rows[0].row_id == 4772
        assert plain.rows[0].row_id == 4774

    def test_nonexistent_threshold_is_not_found_not_nearest(self, engine):
        base = dict(metric="MismatchLoss", request="Threshold Above",
                    scope="All nodes", frequency_selection="All Frequencies", unit="dB")
        result = engine.lookup_by_intent(QueryIntent(**base, params=ThresholdParams(threshold=0.2)))
        assert result.kind == NOT_FOUND

    def test_intent_lookup_never_ambiguous(self, engine):
        """A valid intent (parsed from any row) always resolves to exactly one row."""
        sampled = 0
        for key, rows in engine._intent_index.items():
            row = rows[0]
            intent = intent_from_row(row)
            result = engine.lookup_by_intent(intent)
            assert result.kind == EXACT_MATCH, (
                f"Row {row.row_id} did not resolve exactly: {result.message}"
            )
            assert result.rows[0].row_id == row.row_id
            sampled += 1
            if sampled >= 500:
                break


# ---------------------------------------------------------------------------
# E. Every duplicate family from the audit resolves exactly (requirement 9)
# ---------------------------------------------------------------------------


class TestEveryDuplicateFamily:
    def test_every_row_of_every_duplicate_key_resolves_exactly(self, engine):
        """Exhaustive: all 417 rows across all 80 duplicate 5-field keys.

        Each row's own QUESTION is parsed into a QueryIntent; the intent
        lookup must return EXACTLY that row.
        """
        checked_rows = 0
        checked_keys = 0
        for key5, rows in engine._index.items():
            if len(rows) <= 1:
                continue
            checked_keys += 1
            for row in rows:
                intent = intent_from_row(row)
                # The intent key must differ between rows sharing key5
                result = engine.lookup_by_intent(intent)
                assert result.kind == EXACT_MATCH, (
                    f"key={key5} row={row.row_id}: {result.message}"
                )
                assert result.rows[0].row_id == row.row_id
                checked_rows += 1
        assert checked_keys == 80, f"Expected 80 duplicate keys, saw {checked_keys}"
        assert checked_rows == 417, f"Expected 417 duplicate rows, saw {checked_rows}"

    def test_duplicate_key_rows_have_distinct_intent_keys(self, engine):
        """Within every duplicate 5-field key, all rows get distinct intent keys."""
        for key5, rows in engine._index.items():
            if len(rows) <= 1:
                continue
            intent_keys = [intent_from_row(r).intent_key() for r in rows]
            assert len(set(intent_keys)) == len(rows), (
                f"Rows {[(r.row_id) for r in rows]} share intent keys under {key5}"
            )

    @pytest.mark.parametrize("metric", [
        "CCOMP", "CGAIN", "CNDR", "CNF", "CNP", "CP", "DCP", "GAIN",
        "IP1DB", "IPSAT", "MismatchLoss", "NDCP", "OP1DB", "OPSAT", "TNP",
    ])
    def test_threshold_families_per_metric(self, engine, metric):
        """Each of the 15 metrics has Threshold Above/Below dup keys; every
        threshold value in them must resolve exactly via intent."""
        for request in ("Threshold Above", "Threshold Below"):
            keys = [k for k in engine._index
                    if k[0] == metric and k[1] == request and len(engine._index[k]) > 1]
            for k in keys:
                for row in engine._index[k]:
                    intent = intent_from_row(row)
                    result = engine.lookup_by_intent(intent)
                    assert result.kind == EXACT_MATCH
                    assert result.rows[0].row_id == row.row_id

    @pytest.mark.parametrize("freq", [
        "1200 MHz", "1300 MHz", "1500 MHz", "2100 MHz", "3000 MHz",
    ])
    def test_tnp_ifpd_family_per_frequency(self, engine, freq):
        """TNP individual-frequency power difference dup keys at spot freqs."""
        key5 = ("TNP", "Individual frequency power difference",
                "Final node", freq, "dBm")
        rows = engine._index.get(key5, [])
        assert len(rows) == 2, f"Expected exactly 2 rows for {key5}, got {len(rows)}"
        by_qualifier = {}
        for row in rows:
            intent = intent_from_row(row)
            result = engine.lookup_by_intent(intent)
            assert result.kind == EXACT_MATCH
            assert result.rows[0].row_id == row.row_id
            by_qualifier[intent.params.reference_qualifier] = row.row_id
        # Desired row is listed before the plain row in the catalogue
        assert by_qualifier[ReferenceQualifier.DESIRED_CHANNEL_POWER] < \
            by_qualifier[ReferenceQualifier.CHANNEL_POWER]

    def test_non_duplicate_family_rows_still_resolve(self, engine):
        """Non-duplicate keys (single-row) resolve exactly via intent too."""
        for key5 in [
            ("CGAIN", "Best", "Final node", "All Frequencies", "dB"),
            ("GAIN", "Individual frequency final value", "Final node", "1500 MHz", "dB"),
            ("DCP", "Particular frequency", "Final node", "1200 MHz", "dBm"),
            ("NDCP", "Power difference", "Final node", "All Frequencies", "dBm"),
            ("CP", "Power difference", "Final node", "All Frequencies", "dBm"),
            ("OP1DB", "Power difference", "Final node", "All Frequencies", "dBm"),
        ]:
            row = engine._index[key5][0]
            result = engine.lookup_by_intent(intent_from_row(row))
            assert result.kind == EXACT_MATCH
            assert result.rows[0].row_id == row.row_id


# ---------------------------------------------------------------------------
# F. Office-review documentation (requirement 7)
# ---------------------------------------------------------------------------


class TestOfficeReview:
    def test_office_review_items_exist_and_are_verbatim_grounded(self):
        assert len(OFFICE_REVIEW_ITEMS) >= 4
        for item in OFFICE_REVIEW_ITEMS:
            assert {"item", "observed_wording", "question"} <= set(item)
            assert item["observed_wording"], "verbatim wording must be preserved"

    def test_desired_vs_plain_is_flagged_for_office(self):
        items = " ".join(i["item"] for i in OFFICE_REVIEW_ITEMS)
        assert "desired channel power vs channel power" in items
