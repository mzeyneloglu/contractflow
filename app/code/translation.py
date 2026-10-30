import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from app.code.analyzer import CodeEvidence, QueryEvidence
from app.core.models.documents import SourceLocation, SourceReference
from app.core.models.verification import BusinessRule

MYBATIS_PARAMETER = re.compile(r"#\{\s*(\w+)[^}]*\}")
MYBATIS_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")
CONDITIONAL_BLOCK = re.compile(r"<(if|when)\b[^>]*>([\s\S]*?)</\1>", re.IGNORECASE)
NAMED_BIND = re.compile(r"(?<![:\w]):(\w+)")


@dataclass
class SqlCandidate:
    sql: str
    binds: dict[str, object]
    source: QueryEvidence
    notes: list[str] = field(default_factory=list)


class CandidateRejected(Exception):
    pass


def resolve_mybatis(raw: str, provided: set[str]) -> tuple[str, list[str]]:
    notes: list[str] = []

    def keep_block(match: re.Match) -> str:
        parameters = set(MYBATIS_PARAMETER.findall(match.group(2)))
        if parameters and parameters <= provided:
            return match.group(2)
        notes.append(f"Koşullu SQL bloğu API'de karşılığı olmadığı için dahil edilmedi: {' '.join(match.group(2).split())[:80]}")
        return " "

    text = CONDITIONAL_BLOCK.sub(keep_block, raw)
    text = re.sub(r"<where\b[^>]*>([\s\S]*?)</where>", lambda match: " WHERE " + re.sub(r"^\s*(AND|OR)\b", "", match.group(1).strip(), flags=re.IGNORECASE) if match.group(1).strip() else " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!\[CDATA\[|\]\]>", "", re.sub(r"<[^>]+>", " ", text))
    return MYBATIS_PARAMETER.sub(lambda match: f":{match.group(1)}", text), notes


def qualify_tables(statement: exp.Expression, known_tables: dict[str, str]) -> list[str]:
    unresolved: list[str] = []
    cte_names = {cte.alias_or_name.upper() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        name = table.name.upper()
        if table.db or name in cte_names or name == "DUAL":
            continue
        schema = known_tables.get(name)
        if schema:
            table.set("db", exp.to_identifier(schema))
        else:
            unresolved.append(name)
    return unresolved


def count_wrapper(query: QueryEvidence, api_values: dict[str, str], known_tables: dict[str, str]) -> SqlCandidate:
    if not query.native:
        raise CandidateRejected("JPQL doğrudan çalıştırılamaz; entity eşlemesi üzerinden değerlendirilecek")
    text, notes = (resolve_mybatis(query.raw or query.text, set(api_values)) if query.kind == "mybatis" else (query.text, []))
    if MYBATIS_SUBSTITUTION.search(text):
        raise CandidateRejected("SQL metin birleştirmesi (${...}) içeriyor; güvenli biçimde yeniden üretilemez")
    if re.search(r"\?\d*", text):
        raise CandidateRejected("Pozisyonel parametreler (?) isimle eşlenemiyor")
    try:
        statement = sqlglot.parse_one(text, read="oracle")
    except (ParseError, TokenError) as error:
        raise CandidateRejected(f"SQL ayrıştırılamadı: {str(error).splitlines()[0]}") from error
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise CandidateRejected("Kod sorgusu SELECT değil")
    unresolved = qualify_tables(statement, known_tables)
    if unresolved:
        raise CandidateRejected(f"İzinli şemalarda bulunamayan tablolar: {', '.join(sorted(set(unresolved)))}")
    statement.set("order", None)
    inner = statement.sql(dialect="oracle")
    lookup = {name.casefold(): value for name, value in api_values.items()}
    binds: dict[str, object] = {}
    for name in dict.fromkeys(NAMED_BIND.findall(inner)):
        value = lookup.get(name.casefold())
        binds[name] = int(value) if isinstance(value, str) and re.fullmatch(r"-?\d+", value) else value
        if value is None:
            notes.append(f":{name} parametresi API isteğinde yok; NULL olarak bağlandı")
    return SqlCandidate(sql=f"SELECT COUNT(*) AS RESULT_VALUE FROM ({inner}) CF_SOURCE", binds=binds, source=query, notes=notes)


def code_reference(file: str, line: int, symbol: str, excerpt: str) -> SourceReference:
    return SourceReference(
        document_id="code",
        chunk_id=f"code:{file}:{line}",
        source_filename=file,
        location=SourceLocation(line_start=line, section_path=[symbol] if symbol else []),
        excerpt=excerpt[:400],
    )


def evidence_rules(evidence: CodeEvidence) -> list[BusinessRule]:
    rules: list[BusinessRule] = []
    for query in evidence.queries:
        compact = " ".join(query.text.split())
        rules.append(BusinessRule(text=f"Kod ({query.kind} sorgu): {compact[:220]}", applicability="Servisin veri erişim sorgusu", sources=[code_reference(query.location.file, query.location.line, query.location.symbol, compact)]))
    for derived in evidence.derived_queries:
        conditions = [f"{condition.column or condition.property} {condition.operator}" for condition in derived.conditions]
        rules.append(BusinessRule(text=f"Kod (türetilmiş sorgu {derived.method}): {' AND '.join(conditions)}", applicability=f"Entity {derived.entity}", conditions=conditions, sources=[code_reference(derived.location.file, derived.location.line, derived.location.symbol, derived.method)]))
    for entity in evidence.entities:
        table = f"{entity.schema_name}.{entity.table}" if entity.schema_name else entity.table
        mapping = ", ".join(f"{field}→{column}" for field, column in list(entity.columns.items())[:20])
        text = f"Kod (entity {entity.name}): tablo {table}" + (f"; her sorguya eklenen kısıt: {entity.restriction}" if entity.restriction else "")
        rules.append(BusinessRule(text=text, applicability="Entity eşlemesi", conditions=[entity.restriction] if entity.restriction else [], exceptions=[], sources=[code_reference(entity.location.file, entity.location.line, entity.name, f"{text}. Alanlar: {mapping}")]))
    return rules


POSITIONAL = re.compile(r"\?|(?<![:\w]):(\d+)\b")


def normalize_placeholders(sql: str) -> str:
    counter = iter(range(1, 10_000))
    return POSITIONAL.sub(lambda match: f":p{match.group(1) or next(counter)}", sql)


def is_count_query(statement: exp.Expression) -> bool:
    return isinstance(statement, exp.Select) and len(statement.expressions) == 1 and bool(statement.expressions[0].find(exp.Count))


def runtime_candidate(raw_sql: str, ordered_values: list[str], known_tables: dict[str, str], captured_binds: list[str | None] | None = None) -> tuple[str, dict[str, object], list[str]]:
    notes: list[str] = []
    try:
        statement = sqlglot.parse_one(normalize_placeholders(raw_sql), read="oracle")
    except (ParseError, TokenError) as error:
        raise CandidateRejected(f"Yakalanan SQL ayrıştırılamadı: {str(error).splitlines()[0]}") from error
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise CandidateRejected("Yakalanan ifade SELECT değil")
    unresolved = qualify_tables(statement, known_tables)
    if unresolved:
        raise CandidateRejected(f"İzinli şemalarda bulunamayan tablolar: {', '.join(sorted(set(unresolved)))}")
    for clause in ("order", "limit", "offset", "fetch"):
        statement.set(clause, None)
    inner = statement.sql(dialect="oracle")
    sql = inner if is_count_query(statement) else f"SELECT COUNT(*) AS RESULT_VALUE FROM ({inner}) CF_SOURCE"
    names = list(dict.fromkeys(NAMED_BIND.findall(sql)))
    captured = [value for value in (captured_binds or [])]
    if names and len(captured) >= len(names) and all(captured[index] is not None for index in range(len(names))):
        values = captured[: len(names)]
        notes.append("Bind değerleri Oracle'ın yakaladığı değerlerden alındı (V$SQL_BIND_CAPTURE)")
    elif len(names) == len(ordered_values):
        values = list(ordered_values)
        if names:
            notes.append("Bind değerleri API filtre sırasına göre eşlendi (sezgisel)")
    else:
        raise CandidateRejected(f"Yakalanan sorguda {len(names)} parametre var, API'de {len(ordered_values)} filtre var; güvenli eşleme yapılamadı")
    binds = {name: int(value) if isinstance(value, str) and re.fullmatch(r"-?\d+", value) else value for name, value in zip(names, values, strict=True)}
    return sql, binds, notes
