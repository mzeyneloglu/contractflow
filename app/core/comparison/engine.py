from dataclasses import dataclass
from decimal import Decimal

from app.core.models.common import RunStatus
from app.core.models.verification import (
    ApiObservation,
    AssertionResult,
    AssertionStatus,
    DatabaseObservation,
)


@dataclass(frozen=True)
class Measurement:
    label: str
    value: Decimal | None
    status_if_missing: AssertionStatus
    reason_if_missing: str


@dataclass(frozen=True)
class ComparisonOutcome:
    assertions: list[AssertionResult]
    status: RunStatus
    coverage_notes: list[str]


def measure_api(observation: ApiObservation | None) -> Measurement:
    if observation is None:
        return Measurement("API", None, AssertionStatus.SKIPPED, "API gözlemi yok")
    if observation.error_code:
        return Measurement("API", None, AssertionStatus.ERROR, f"API hatası: {observation.error_code}")
    if not observation.complete:
        return Measurement("API", None, AssertionStatus.INCONCLUSIVE, f"API kayıt seti eksik: {observation.incomplete_reason}")
    if observation.missing_id_count:
        return Measurement("API", None, AssertionStatus.INCONCLUSIVE, f"{observation.missing_id_count} kayıtta ID alanı yok")
    count = observation.measured_count
    return Measurement("API", None if count is None else Decimal(count), AssertionStatus.INCONCLUSIVE, "API adedi ölçülemedi")


def measure_database(observation: DatabaseObservation | None) -> Measurement:
    if observation is None:
        return Measurement("Oracle", None, AssertionStatus.SKIPPED, "Oracle gözlemi yok")
    if observation.error_code:
        return Measurement("Oracle", None, AssertionStatus.ERROR, f"Oracle hatası: {observation.error_code}")
    if observation.truncated:
        return Measurement("Oracle", None, AssertionStatus.INCONCLUSIVE, "Oracle sonucu sınırda kesildi")
    value = observation.scalar
    if value is None:
        return Measurement("Oracle", None, AssertionStatus.INCONCLUSIVE, "Oracle sorgusu tek bir sayısal değer döndürmedi")
    return Measurement("Oracle", value, AssertionStatus.INCONCLUSIVE, "")


def measure_expectation(expected: int | None) -> Measurement:
    if expected is None:
        return Measurement("Beklenti", None, AssertionStatus.SKIPPED, "Kullanıcı beklentisi belirtilmedi")
    return Measurement("Beklenti", Decimal(expected), AssertionStatus.SKIPPED, "")


def _format(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _missing_status(left: Measurement, right: Measurement) -> tuple[AssertionStatus, str] | None:
    missing = [measurement for measurement in (left, right) if measurement.value is None]
    if not missing:
        return None
    severity = [AssertionStatus.ERROR, AssertionStatus.INCONCLUSIVE, AssertionStatus.SKIPPED]
    worst = min(missing, key=lambda measurement: severity.index(measurement.status_if_missing))
    return worst.status_if_missing, "; ".join(measurement.reason_if_missing for measurement in missing)


def assert_equal(name: str, title: str, left: Measurement, right: Measurement, mismatch_message: str) -> AssertionResult:
    missing = _missing_status(left, right)
    if missing:
        status, message = missing
    elif left.value == right.value:
        status, message = AssertionStatus.PASS, f"{left.label} ve {right.label} aynı değeri veriyor"
    else:
        status, message = AssertionStatus.FAIL, mismatch_message
    return AssertionResult(
        name=name,
        title=title,
        status=status,
        left_label=left.label,
        left_value=_format(left.value),
        right_label=right.label,
        right_value=_format(right.value),
        message=message,
    )


def overall_status(assertions: list[AssertionResult]) -> RunStatus:
    statuses = {assertion.status for assertion in assertions}
    if AssertionStatus.ERROR in statuses:
        return RunStatus.ERROR
    if AssertionStatus.INCONCLUSIVE in statuses:
        return RunStatus.INCONCLUSIVE
    if AssertionStatus.FAIL in statuses:
        return RunStatus.FAIL
    if AssertionStatus.PASS in statuses:
        return RunStatus.PASS
    return RunStatus.INCONCLUSIVE


def api_coverage_notes(observation: ApiObservation | None) -> list[str]:
    if observation is None or observation.error_code:
        return []
    notes = [f"API: {observation.pages_fetched} sayfa, {observation.fetched_count} kayıt getirildi"]
    if observation.reported_total is not None:
        notes.append(f"API'nin bildirdiği toplam: {observation.reported_total}")
        if observation.complete and observation.unique_id_count is not None and observation.reported_total != observation.unique_id_count:
            notes.append("Bildirilen toplam ile benzersiz ID sayısı farklı; adet benzersiz ID üzerinden ölçüldü")
    if observation.duplicate_ids:
        notes.append(f"Sayfalar arasında tekrar eden ID'ler: {', '.join(observation.duplicate_ids[:10])}")
    if not observation.complete:
        notes.append(f"Tam kayıt seti doğrulanamadı: {observation.incomplete_reason}")
    return notes


def measure_implementation(observation: DatabaseObservation | None) -> Measurement:
    measured = measure_database(observation)
    reason = measured.reason_if_missing.replace("Oracle", "Servis sorgusu")
    if observation is None:
        return Measurement("Servis sorgusu", None, AssertionStatus.SKIPPED, "Servisin çalıştırdığı sorgu bulunamadı")
    return Measurement("Servis sorgusu", measured.value, measured.status_if_missing, reason)


def compare_counts(
    expected: int | None,
    api: ApiObservation | None,
    database: DatabaseObservation | None,
    implementation: DatabaseObservation | None = None,
) -> ComparisonOutcome:
    api_measurement = measure_api(api)
    database_measurement = measure_database(database)
    expectation = measure_expectation(expected)
    implementation_measurement = measure_implementation(implementation)
    assertions = [
        assert_equal(
            "api_vs_database",
            "API – iş kuralı sorgusu (Oracle)",
            api_measurement,
            database_measurement,
            "API ve Oracle farklı adet veriyor; kaynaklar tutarsız",
        ),
        assert_equal(
            "api_vs_expectation",
            "API – kullanıcı beklentisi",
            api_measurement,
            expectation,
            "API sonucu kullanıcı beklentisini karşılamıyor",
        ),
        assert_equal(
            "database_vs_expectation",
            "Oracle – kullanıcı beklentisi",
            database_measurement,
            expectation,
            "Oracle sonucu kullanıcı beklentisini karşılamıyor",
        ),
        assert_equal(
            "api_vs_implementation",
            "API – servisin kendi sorgusu",
            api_measurement,
            implementation_measurement,
            "API çıktısı servisin çalıştırdığı sorgunun sonucundan farklı; dönüşüm, filtreleme veya sayfalama katmanında fark var",
        ),
        assert_equal(
            "database_vs_implementation",
            "İş kuralı sorgusu – servisin kendi sorgusu",
            database_measurement,
            implementation_measurement,
            "Servisin sorgusu iş kuralı sorgusundan farklı adet veriyor; iş mantığı uygulaması belgeden/kuraldan sapıyor olabilir",
        ),
    ]
    notes = api_coverage_notes(api)
    if database is not None and database.truncated:
        notes.append("Oracle sonucu satır/byte limitinde kesildi")
    return ComparisonOutcome(assertions=assertions, status=overall_status(assertions), coverage_notes=notes)


def triangulate(outcome: ComparisonOutcome) -> str:
    by_name = {assertion.name: assertion.status for assertion in outcome.assertions}
    passed, failed = AssertionStatus.PASS, AssertionStatus.FAIL
    api_spec, api_impl, spec_impl = by_name.get("api_vs_database"), by_name.get("api_vs_implementation"), by_name.get("database_vs_implementation")
    if api_impl is None or AssertionStatus.SKIPPED in {api_impl, spec_impl}:
        return ""
    if api_spec is passed and api_impl is passed and spec_impl is passed:
        return "Üç kaynak tutarlı: API, servisin kendi sorgusu ve bağımsız iş kuralı sorgusu aynı adedi veriyor"
    if api_impl is passed and spec_impl is failed:
        return "Teşhis: servis kendi sorgusunun sonucunu doğru döndürüyor, ancak sorgunun iş mantığı bağımsız iş kuralı sorgusundan farklı (iş mantığı/filtre farkı)"
    if api_impl is failed and spec_impl is passed:
        return "Teşhis: servisin sorgusu iş kuralıyla uyumlu, ancak API çıktısı bu sorgudan farklı (dönüşüm, mapping veya sayfalama katmanı)"
    if api_impl is failed and spec_impl is failed and api_spec is passed:
        return "Teşhis: API iş kuralıyla uyumlu, ancak yakalanan servis sorgusu farklı; yakalanan sorgu eksik veya başka bir adıma ait olabilir"
    if failed in {api_impl, spec_impl}:
        return "Teşhis: üç kaynak birbirinden farklı; servis sorgusu, API dönüşümü ve iş kuralı ayrı ayrı incelenmeli"
    return ""


def interpret(outcome: ComparisonOutcome) -> str:
    diagnosis = triangulate(outcome)
    base = interpret_pair(outcome)
    return f"{base}. {diagnosis}" if diagnosis else base


def interpret_pair(outcome: ComparisonOutcome) -> str:
    by_name = {assertion.name: assertion.status for assertion in outcome.assertions}
    consistent = by_name["api_vs_database"]
    api_ok = by_name["api_vs_expectation"]
    database_ok = by_name["database_vs_expectation"]
    passed, failed = AssertionStatus.PASS, AssertionStatus.FAIL
    if consistent is passed and api_ok is passed and database_ok is passed:
        return "Tanımlı kontroller geçti"
    if consistent is passed and api_ok is failed and database_ok is failed:
        return "Kaynaklar tutarlı; beklenen adet sağlanmıyor"
    if consistent is failed and api_ok is failed and database_ok is passed:
        return "API–Oracle tutarsız; API beklentiyi karşılamıyor"
    if consistent is failed and api_ok is passed and database_ok is failed:
        return "API beklentiyi sağlıyor; Oracle ile tutarsız"
    if consistent is passed and api_ok is AssertionStatus.SKIPPED:
        return "API ve Oracle tutarlı; kullanıcı beklentisi verilmedi"
    if consistent is failed:
        return "API ve Oracle farklı adet veriyor"
    return "Kanıtlar kesin hüküm için yeterli değil"
