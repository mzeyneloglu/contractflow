import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.code.repository import CodeIndex, SourceFile

MAX_DEPTH = 4
MAX_METHODS = 40
MAX_DEFINITIONS_PER_CALL = 4
ANNOTATION_LOOKBACK = 25
ROUTE_ANNOTATION = re.compile(r"@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|Path|Get|Controller|RestController)\b(\s*\(([^)]*)\))?")
STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
CONSTANT = re.compile(r"\bstatic\s+final\s+String\s+(\w+)\s*=\s*(.+?);")
CLASS_DECLARATION = re.compile(r"\b(class|interface|object)\s+(\w+)")
METHOD_DECLARATION = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|protected|private|static|final|abstract|default|synchronized|override|suspend|open|fun)\s+)*(?:<[^>]+>\s*)?(?:[\w.<>\[\],?\s]+\s+)?(\w+)\s*\(")
CALL = re.compile(r"(?:\b(\w+)\s*\.\s*)?\b(\w+)\s*\(")
FIELD_DECLARATION = re.compile(r"(?:private|protected|public|val|var|final)\s+(?:final\s+)?(\w+)(?:<[^>]*>)?\s+(\w+)\s*[;=]")
REPOSITORY_BASE = re.compile(r"\b(?:JpaRepository|CrudRepository|PagingAndSortingRepository|ListCrudRepository|ReactiveCrudRepository|Repository)\s*<\s*(\w+)")
SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
SQL_HINT = re.compile(r"\bselect\b[\s\S]+\bfrom\b", re.IGNORECASE)
PROCEDURE_HINT = re.compile(r"@Procedure|\{\s*call\s+|StoredProcedureQuery|SimpleJdbcCall|createStoredProcedureQuery", re.IGNORECASE)
DERIVED_PREFIX = re.compile(r"^(find|read|get|query|search|stream|count|exists)(All|Distinct|First\d*|Top\d*)*By(?=[A-Z])")
DERIVED_OPERATORS = (
    ("IsNotNull", "IS NOT NULL"), ("NotNull", "IS NOT NULL"), ("IsNull", "IS NULL"), ("Null", "IS NULL"),
    ("IsTrue", "= TRUE"), ("True", "= TRUE"), ("IsFalse", "= FALSE"), ("False", "= FALSE"),
    ("NotIn", "NOT IN"), ("In", "IN"), ("NotLike", "NOT LIKE"), ("Like", "LIKE"), ("Containing", "LIKE"),
    ("StartingWith", "LIKE"), ("EndingWith", "LIKE"), ("GreaterThanEqual", ">="), ("GreaterThan", ">"),
    ("LessThanEqual", "<="), ("LessThan", "<"), ("Between", "BETWEEN"), ("After", ">"), ("Before", "<"),
    ("IsNot", "<>"), ("Not", "<>"), ("Equals", "="), ("Is", "="),
)
CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized", "super", "this", "throw", "else", "try", "assert"}


class CodeLocation(BaseModel):
    file: str
    line: int
    symbol: str = ""

    def label(self) -> str:
        return f"{self.file}:{self.line}" + (f" ({self.symbol})" if self.symbol else "")


class QueryEvidence(BaseModel):
    kind: str
    text: str
    native: bool
    location: CodeLocation
    dynamic: bool = False
    raw: str = ""


class DerivedCondition(BaseModel):
    property: str
    operator: str
    column: str = ""


class DerivedQueryEvidence(BaseModel):
    method: str
    entity: str
    conditions: list[DerivedCondition]
    location: CodeLocation


class EntityEvidence(BaseModel):
    name: str
    table: str
    schema_name: str = ""
    restriction: str = ""
    columns: dict[str, str] = Field(default_factory=dict)
    inferred_columns: list[str] = Field(default_factory=list)
    location: CodeLocation


class CodeEvidence(BaseModel):
    route: CodeLocation | None = None
    route_path: str = ""
    chain: list[CodeLocation] = Field(default_factory=list)
    queries: list[QueryEvidence] = Field(default_factory=list)
    derived_queries: list[DerivedQueryEvidence] = Field(default_factory=list)
    entities: list[EntityEvidence] = Field(default_factory=list)
    procedures: list[CodeLocation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.queries or self.derived_queries or self.entities)

    def tables(self) -> list[str]:
        return list(dict.fromkeys(f"{entity.schema_name}.{entity.table}" if entity.schema_name else entity.table for entity in self.entities))


@dataclass
class MethodRef:
    source: SourceFile
    line: int
    name: str
    class_name: str
    body: str = ""
    body_end: int = 0


@dataclass
class Analyzer:
    index: CodeIndex
    constants: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for source in self.index.by_suffix(".java", ".kt"):
            for line in source.lines:
                match = CONSTANT.search(line)
                if match:
                    literals = STRING_LITERAL.findall(match.group(2))
                    if literals:
                        self.constants[match.group(1)] = "".join(literals)

    def resolve_strings(self, expression: str) -> list[str]:
        literals = STRING_LITERAL.findall(expression)
        if literals:
            return literals
        names = re.findall(r"\b([A-Z][A-Z0-9_]+)\b", expression)
        return [self.constants[name] for name in names if name in self.constants]


def segments(path: str) -> list[str]:
    return [segment for segment in path.strip("/").split("/") if segment]


def segment_matches(expected: str, actual: str) -> bool:
    if expected.startswith("{") and actual.startswith("{"):
        return True
    return expected.casefold() == actual.casefold()


def suffix_match(openapi_path: str, route: str) -> int:
    expected, actual = segments(openapi_path), segments(route)
    if not actual or len(actual) > len(expected):
        return 0
    tail = expected[-len(actual):]
    return len(actual) if all(segment_matches(left, right) for left, right in zip(tail, actual, strict=True)) else 0


def annotation_text(lines: list[str], start: int) -> str:
    text, depth = "", 0
    for line in lines[start : start + 12]:
        text += line.strip() + " "
        depth += line.count("(") - line.count(")")
        if depth <= 0 and "(" in text:
            break
    return text


def declaration_after(lines: list[str], start: int) -> tuple[int, str] | None:
    for offset in range(start, min(start + 15, len(lines))):
        stripped = lines[offset].strip()
        if not stripped or stripped.startswith("@") or stripped.startswith("//") or stripped.startswith("*"):
            continue
        class_match = CLASS_DECLARATION.search(stripped)
        if class_match:
            return offset, "class:" + class_match.group(2)
        method_match = METHOD_DECLARATION.match(lines[offset])
        if method_match and method_match.group(1) not in CONTROL_WORDS:
            return offset, method_match.group(1)
    return None


def http_method_allowed(annotation: str, kind: str, method: str) -> bool:
    if kind in {"GetMapping", "Get"}:
        return method == "GET"
    if kind.endswith("Mapping") and kind != "RequestMapping":
        return kind.upper().startswith(method)
    if "RequestMethod." in annotation:
        return f"RequestMethod.{method}" in annotation
    return True


def body_of(lines: list[str], start: int) -> tuple[str, int]:
    depth, opened, collected = 0, False, []
    for number in range(start, min(start + 400, len(lines))):
        line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', lines[number])
        collected.append(lines[number])
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            return "\n".join(collected), number
        if not opened and line.rstrip().endswith(";"):
            return "\n".join(collected), number
    return "\n".join(collected), start + len(collected) - 1


def class_name_at(source: SourceFile, line: int) -> str:
    for number in range(line, -1, -1):
        match = CLASS_DECLARATION.search(source.lines[number])
        if match:
            return match.group(2)
    return source.path.stem


def find_route(analyzer: Analyzer, openapi_path: str, method: str) -> MethodRef | None:
    best: tuple[int, MethodRef] | None = None
    for source in analyzer.index.by_suffix(".java", ".kt"):
        class_prefixes = [""]
        for number, line in enumerate(source.lines):
            match = ROUTE_ANNOTATION.search(line)
            if not match:
                continue
            kind = match.group(1)
            annotation = annotation_text(source.lines, number) if match.group(2) else line.strip()
            declared = declaration_after(source.lines, number + 1)
            if declared is None:
                continue
            paths = analyzer.resolve_strings(annotation[annotation.find("(") :]) if "(" in annotation else []
            if declared[1].startswith("class:"):
                class_prefixes = paths or [""]
                continue
            if kind in {"Controller", "RestController"} or not http_method_allowed(annotation, kind, method):
                continue
            for prefix in class_prefixes:
                for path in paths or [""]:
                    score = suffix_match(openapi_path, f"{prefix}/{path}")
                    if score and (best is None or score > best[0]):
                        body, end = body_of(source.lines, declared[0])
                        best = (score, MethodRef(source, declared[0], declared[1], class_name_at(source, declared[0]), body, end))
    return best[1] if best else None


def field_types(source: SourceFile) -> dict[str, str]:
    types: dict[str, str] = {}
    for line in source.lines:
        for match in FIELD_DECLARATION.finditer(line):
            types[match.group(2)] = match.group(1)
    return types


def definitions(analyzer: Analyzer, name: str, preferred_type: str) -> list[MethodRef]:
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
    found: list[MethodRef] = []
    for source in analyzer.index.by_suffix(".java", ".kt"):
        for number, line in enumerate(source.lines):
            if not pattern.search(line):
                continue
            match = METHOD_DECLARATION.match(line)
            if not match or match.group(1) != name or re.search(r"[=.]\s*" + re.escape(name) + r"\s*\(", line):
                continue
            body, end = body_of(source.lines, number)
            found.append(MethodRef(source, number, name, class_name_at(source, number), body, end))
    if preferred_type:
        root = preferred_type.removesuffix("Impl")
        found.sort(key=lambda ref: (root not in ref.class_name, ref.source.relative))
    return found[:MAX_DEFINITIONS_PER_CALL]


def calls_in(method: MethodRef) -> list[tuple[str, str]]:
    body = method.body.split("{", 1)[1] if "{" in method.body else ""
    return [(receiver or "", name) for receiver, name in CALL.findall(body) if name not in CONTROL_WORDS and not name[0].isupper()]


def literal_concatenation(text: str) -> str:
    blocks = re.findall(r'"""([\s\S]*?)"""', text)
    if blocks:
        return " ".join(blocks)
    return " ".join(STRING_LITERAL.findall(text))


def annotations_before(source: SourceFile, line: int) -> str:
    start = line
    while start > 0 and line - start < ANNOTATION_LOOKBACK:
        previous = source.lines[start - 1].strip()
        if previous.endswith(";") or previous.endswith("}") or previous.endswith("{"):
            break
        start -= 1
    return "\n".join(source.lines[start:line])


def query_evidence(analyzer: Analyzer, method: MethodRef, evidence: CodeEvidence) -> None:
    location = CodeLocation(file=method.source.relative, line=method.line + 1, symbol=f"{method.class_name}.{method.name}")
    annotations = annotations_before(method.source, method.line)
    for kind in ("Query", "NativeQuery", "Select"):
        position = annotations.find(f"@{kind}")
        if position >= 0:
            text = literal_concatenation(annotations[position:])
            if text.strip():
                native = kind != "Query" or bool(re.search(r"nativeQuery\s*=\s*true", annotations[position:]))
                evidence.queries.append(QueryEvidence(kind="mybatis" if kind == "Select" else ("native" if native else "jpql"), text=text, native=native, location=location))
    if PROCEDURE_HINT.search(annotations + method.body):
        evidence.procedures.append(location)
    for literal in STRING_LITERAL.findall(method.body) + re.findall(r'"""([\s\S]*?)"""', method.body):
        if SQL_HINT.search(literal) and SQL_START.search(literal):
            evidence.queries.append(QueryEvidence(kind="jdbc", text=literal, native=True, location=location))
    mybatis_xml(analyzer, method, location, evidence)
    derived_query(analyzer, method, location, evidence)


def mybatis_xml(analyzer: Analyzer, method: MethodRef, location: CodeLocation, evidence: CodeEvidence) -> None:
    if not method.body.rstrip().endswith(";"):
        return
    pattern = re.compile(rf'<select[^>]*\bid\s*=\s*"{re.escape(method.name)}"[^>]*>([\s\S]*?)</select>', re.IGNORECASE)
    for source in analyzer.index.by_suffix(".xml"):
        match = pattern.search(source.text)
        if not match:
            continue
        raw = match.group(1)
        dynamic = bool(re.search(r"<(if|choose|where|foreach|trim|set)\b", raw))
        text = re.sub(r"<!\[CDATA\[|\]\]>", "", re.sub(r"<[^>]+>", " ", raw))
        line = source.text[: match.start()].count("\n") + 1
        evidence.queries.append(QueryEvidence(kind="mybatis", text=text, native=True, dynamic=dynamic, raw=raw, location=CodeLocation(file=source.relative, line=line, symbol=method.name)))


def derived_query(analyzer: Analyzer, method: MethodRef, location: CodeLocation, evidence: CodeEvidence) -> None:
    prefix = DERIVED_PREFIX.match(method.name)
    if not prefix or not method.body.rstrip().endswith(";"):
        return
    entity = repository_entity(method.source)
    criteria = method.name[prefix.end() :].split("OrderBy")[0]
    conditions = []
    for part in re.split(r"(?<=[a-z0-9])(?:And|Or)(?=[A-Z])", criteria):
        operator = "="
        for suffix, sql in DERIVED_OPERATORS:
            if part.endswith(suffix) and len(part) > len(suffix):
                part, operator = part[: -len(suffix)], sql
                break
        conditions.append(DerivedCondition(property=part[0].lower() + part[1:], operator=operator))
    evidence.derived_queries.append(DerivedQueryEvidence(method=method.name, entity=entity, conditions=conditions, location=location))
    if entity:
        add_entity(analyzer, entity, evidence)


def repository_entity(source: SourceFile) -> str:
    match = REPOSITORY_BASE.search(source.text)
    return match.group(1) if match else ""


def snake_case(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()


def add_entity(analyzer: Analyzer, entity: str, evidence: CodeEvidence) -> None:
    if any(existing.name == entity for existing in evidence.entities):
        return
    for source in analyzer.index.by_suffix(".java", ".kt"):
        if not re.search(rf"\bclass\s+{re.escape(entity)}\b", source.text) or "@Entity" not in source.text:
            continue
        table_match = re.search(r'@Table\s*\(([^)]*)\)', source.text)
        table_args = table_match.group(1) if table_match else ""
        name = re.search(r'name\s*=\s*"([^"]+)"', table_args)
        schema = re.search(r'schema\s*=\s*"([^"]+)"', table_args)
        restriction = re.search(r'@(?:Where|SQLRestriction)\s*\(\s*(?:clause\s*=\s*)?"([^"]+)"', source.text)
        columns, inferred = entity_columns(source)
        line = next((number for number, text in enumerate(source.lines, start=1) if re.search(rf"\bclass\s+{re.escape(entity)}\b", text)), 1)
        evidence.entities.append(
            EntityEvidence(
                name=entity,
                table=(name.group(1) if name else snake_case(entity)).upper(),
                schema_name=schema.group(1).upper() if schema else "",
                restriction=restriction.group(1) if restriction else "",
                columns=columns,
                inferred_columns=inferred,
                location=CodeLocation(file=source.relative, line=line, symbol=entity),
            )
        )
        return
    evidence.warnings.append(f"Entity sınıfı bulunamadı: {entity}")


def entity_columns(source: SourceFile) -> tuple[dict[str, str], list[str]]:
    columns: dict[str, str] = {}
    inferred: list[str] = []
    pending = ""
    for line in source.lines:
        stripped = line.strip()
        if stripped.startswith("@"):
            pending += stripped + " "
            continue
        field = re.match(r"(?:private|protected|public)\s+(?:final\s+)?[\w.<>\[\], ?]+\s+(\w+)\s*[;=]", stripped)
        if field and "static" not in stripped:
            column = re.search(r'@(?:Column|JoinColumn)\s*\([^)]*name\s*=\s*"([^"]+)"', pending)
            if "@Transient" not in pending and "@OneToMany" not in pending and "@ManyToMany" not in pending:
                columns[field.group(1)] = (column.group(1) if column else snake_case(field.group(1))).upper()
                if not column:
                    inferred.append(field.group(1))
        if stripped and not stripped.startswith("@"):
            pending = ""
    return columns, inferred


def analyze_operation(index: CodeIndex, openapi_path: str, method: str = "GET") -> CodeEvidence:
    analyzer = Analyzer(index)
    evidence = CodeEvidence(route_path=openapi_path)
    handler = find_route(analyzer, openapi_path, method)
    if handler is None:
        evidence.warnings.append(f"Endpoint için handler bulunamadı: {method} {openapi_path}")
        return evidence
    evidence.route = CodeLocation(file=handler.source.relative, line=handler.line + 1, symbol=f"{handler.class_name}.{handler.name}")
    visited: set[tuple[str, int]] = set()
    frontier = [(handler, 0)]
    while frontier and len(visited) < MAX_METHODS:
        current, depth = frontier.pop(0)
        key = (current.source.relative, current.line)
        if key in visited:
            continue
        visited.add(key)
        evidence.chain.append(CodeLocation(file=current.source.relative, line=current.line + 1, symbol=f"{current.class_name}.{current.name}"))
        query_evidence(analyzer, current, evidence)
        entity = repository_entity(current.source)
        if entity:
            add_entity(analyzer, entity, evidence)
        if depth >= MAX_DEPTH:
            continue
        types = field_types(current.source)
        for receiver, name in calls_in(current):
            for definition in definitions(analyzer, name, types.get(receiver, "")):
                frontier.append((definition, depth + 1))
    for derived in evidence.derived_queries:
        entity = next((item for item in evidence.entities if item.name == derived.entity), None)
        for condition in derived.conditions:
            if entity:
                condition.column = entity.columns.get(condition.property, "")
    return evidence
