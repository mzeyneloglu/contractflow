from decimal import Decimal

import pytest

from app.core.comparison.engine import compare_counts, interpret
from app.core.models.common import ErrorCode, RunStatus
from app.core.models.verification import ApiObservation, AssertionStatus, DatabaseObservation


def api(count: int, **overrides) -> ApiObservation:
    values = dict(operation_id="op", curl="curl", status_code=200, pages_fetched=1, fetched_count=count, unique_id_count=count, complete=True)
    values.update(overrides)
    return ApiObservation(**values)


def database(value, **overrides) -> DatabaseObservation:
    values = dict(sql="SELECT COUNT(*) FROM T", rows=[[value]], row_count=1)
    values.update(overrides)
    return DatabaseObservation(**values)


def statuses(outcome) -> dict[str, AssertionStatus]:
    return {assertion.name: assertion.status for assertion in outcome.assertions}


@pytest.mark.parametrize(
    ("expected", "api_count", "db_count", "run_status", "triple", "summary"),
    [
        (25, 25, 25, RunStatus.PASS, ("PASS", "PASS", "PASS"), "Tanımlı kontroller geçti"),
        (25, 23, 25, RunStatus.FAIL, ("FAIL", "FAIL", "PASS"), "API–Oracle tutarsız; API beklentiyi karşılamıyor"),
        (25, 23, 23, RunStatus.FAIL, ("PASS", "FAIL", "FAIL"), "Kaynaklar tutarlı; beklenen adet sağlanmıyor"),
        (25, 25, 23, RunStatus.FAIL, ("FAIL", "PASS", "FAIL"), "API beklentiyi sağlıyor; Oracle ile tutarsız"),
    ],
)
def test_four_count_combinations_are_independent(expected, api_count, db_count, run_status, triple, summary):
    outcome = compare_counts(expected, api(api_count), database(db_count))
    result = statuses(outcome)
    assert (result["api_vs_database"], result["api_vs_expectation"], result["database_vs_expectation"]) == tuple(AssertionStatus(item) for item in triple)
    assert outcome.status is run_status
    assert interpret(outcome) == summary


@pytest.mark.parametrize(
    ("api_count", "impl_count", "spec_count", "fragment"),
    [
        (25, 25, 25, "Üç kaynak tutarlı"),
        (30, 30, 25, "iş mantığı/filtre farkı"),
        (23, 25, 25, "dönüşüm, mapping veya sayfalama"),
    ],
)
def test_triangulation_locates_the_disagreeing_layer(api_count, impl_count, spec_count, fragment):
    outcome = compare_counts(None, api(api_count), database(spec_count), database(impl_count))
    assert fragment in interpret(outcome)


def test_missing_implementation_keeps_two_way_behaviour():
    outcome = compare_counts(25, api(25), database(25))
    statuses_by_name = statuses(outcome)
    assert statuses_by_name["api_vs_implementation"] is AssertionStatus.SKIPPED
    assert outcome.status is RunStatus.PASS
    assert interpret(outcome) == "Tanımlı kontroller geçti"


def test_incomplete_pagination_is_inconclusive_not_fail():
    outcome = compare_counts(25, api(10, complete=False, incomplete_reason="sayfa limiti"), database(25))
    assert statuses(outcome)["api_vs_database"] is AssertionStatus.INCONCLUSIVE
    assert statuses(outcome)["database_vs_expectation"] is AssertionStatus.PASS
    assert outcome.status is RunStatus.INCONCLUSIVE


def test_truncated_database_result_is_inconclusive():
    outcome = compare_counts(25, api(25), database(25, truncated=True))
    assert statuses(outcome)["api_vs_database"] is AssertionStatus.INCONCLUSIVE
    assert outcome.status is RunStatus.INCONCLUSIVE


def test_connection_error_is_error_not_fail():
    failed_api = ApiObservation(operation_id="op", curl="curl", error_code=ErrorCode.CONNECTION_REFUSED)
    outcome = compare_counts(25, failed_api, database(25))
    assert statuses(outcome)["api_vs_database"] is AssertionStatus.ERROR
    assert outcome.status is RunStatus.ERROR


def test_missing_expectation_skips_expectation_assertions():
    outcome = compare_counts(None, api(7), database(7))
    result = statuses(outcome)
    assert result["api_vs_expectation"] is AssertionStatus.SKIPPED
    assert result["api_vs_database"] is AssertionStatus.PASS
    assert outcome.status is RunStatus.PASS


def test_unique_ids_drive_api_count_not_first_page_length():
    paged = api(30, pages_fetched=3, unique_id_count=25, reported_total=25, duplicate_ids=["7"])
    outcome = compare_counts(25, paged, database(25))
    assert outcome.status is RunStatus.PASS
    assert any("tekrar eden" in note for note in outcome.coverage_notes)


def test_records_without_ids_are_inconclusive():
    outcome = compare_counts(25, api(25, missing_id_count=2), database(25))
    assert statuses(outcome)["api_vs_database"] is AssertionStatus.INCONCLUSIVE


def test_large_decimal_values_keep_precision():
    big = Decimal("12345678901234567890123")
    outcome = compare_counts(None, None, database(big))
    assert outcome.assertions[0].right_value == "12345678901234567890123"


def test_null_database_value_is_inconclusive():
    outcome = compare_counts(5, api(5), database(None))
    assert statuses(outcome)["api_vs_database"] is AssertionStatus.INCONCLUSIVE


def test_multi_row_database_result_is_not_scalar():
    multi = DatabaseObservation(sql="s", rows=[[1], [2]], row_count=2)
    outcome = compare_counts(1, api(1), multi)
    assert statuses(outcome)["database_vs_expectation"] is AssertionStatus.INCONCLUSIVE
