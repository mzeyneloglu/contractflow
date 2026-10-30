from app.core.models.verification import EvidenceReport

STATUS_LABELS = {"PASS": "GEÇTİ", "FAIL": "BAŞARISIZ", "INCONCLUSIVE": "BELİRSİZ", "ERROR": "HATA", "SKIPPED": "ATLANDI", "CANCELLED": "İPTAL"}


def label(status: str) -> str:
    return f"{STATUS_LABELS.get(status, status)} ({status})"


def render_markdown(report: EvidenceReport) -> str:
    lines = [
        f"# ContractFlow doğrulama raporu — {report.run_id}",
        "",
        f"- **Durum:** {label(report.status.value)}",
        f"- **Proje / ortam:** {report.project_name} / {report.environment}",
        f"- **Talep:** {report.user_request}",
        f"- **Zaman aralığı:** {report.started_at.isoformat()} → {report.finished_at.isoformat() if report.finished_at else '-'}",
        f"- **Model / prompt:** {report.model} / {report.prompt_version}",
        f"- **OpenAPI özeti:** `{report.openapi_hash[:16]}`",
        f"- **Belge snapshot'ı:** {', '.join(report.document_snapshot) or 'yok'}",
        "",
        "## Özet",
        "",
        report.summary or "-",
        "",
        "## Bağımsız assertion'lar",
        "",
        "| Kontrol | Durum | Sol | Sağ | Açıklama |",
        "|---|---|---:|---:|---|",
    ]
    for assertion in report.assertions:
        lines.append(f"| {assertion.title} | {label(assertion.status.value)} | {assertion.left_label}: {assertion.left_value or '-'} | {assertion.right_label}: {assertion.right_value or '-'} | {assertion.message} |")
    lines += ["", "## Kullanılan iş kuralları", ""]
    for rule in report.rules.rules if report.rules else []:
        sources = "; ".join(source.label() for source in rule.sources)
        lines.append(f"- {rule.text} — _{sources}_")
    if report.rules and report.rules.unresolved:
        lines += ["", "**Çözülemeyenler:** " + "; ".join(report.rules.unresolved)]
    if report.mapping:
        mapping = report.mapping
        lines += [
            "",
            "## Eşleştirme",
            "",
            f"- Operasyon: `{mapping.operation_id}` → tablo `{mapping.base_table}` (kimlik: {', '.join(mapping.primary_key)})",
            f"- Parametre–kolon: {', '.join(f'{parameter} → {column}' for parameter, column in mapping.parameter_columns.items())}",
            f"- Kapsam filtreleri: {', '.join(f'{item.column} {item.operator.value} {item.values}' for item in mapping.scope_filters) or 'yok'}",
            f"- Onaylı: {'evet' if mapping.confirmed else 'hayır'}",
        ]
    if report.api:
        api = report.api
        lines += [
            "",
            "## API gözlemi",
            "",
            "```bash",
            api.curl,
            "```",
            "",
            f"- HTTP durum: {api.status_code}, sayfa: {api.pages_fetched}, getirilen: {api.fetched_count}, benzersiz ID: {api.unique_id_count}, bildirilen toplam: {api.reported_total}",
            f"- Tamamlandı: {'evet' if api.complete else 'hayır'} {api.incomplete_reason}",
        ]
        if api.error_code:
            lines.append(f"- Hata: {api.error_code.value} {api.error_message}")
        if api.schema_errors:
            lines.append(f"- Şema uyumsuzlukları: {'; '.join(api.schema_errors)}")
    if report.database:
        database = report.database
        lines += ["", "## Oracle gözlemi", "", "```sql", database.sql, "```", "", f"- Bind'lar: `{database.binds}`", f"- Satır: {database.row_count}, kesildi: {'evet' if database.truncated else 'hayır'}, süre: {database.elapsed_ms} ms"]
        if database.error_code:
            lines.append(f"- Hata: {database.error_code.value} {database.error_message}")
    if report.coverage_notes:
        lines += ["", "## Kapsam ve eksikler", ""] + [f"- {note}" for note in report.coverage_notes]
    if report.hypotheses:
        lines += ["", "## Hipotezler (kesin kök neden değildir)", ""] + [f"- {item}" for item in report.hypotheses]
    lines += ["", "## Araç izi", ""]
    for event in report.tool_events:
        lines.append(f"- `{event.agent}` → `{event.tool}` {'✓' if event.ok else '✗'} {event.summary}")
    return "\n".join(lines) + "\n"
