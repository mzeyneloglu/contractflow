from pathlib import Path

import pytest

from app.code.analyzer import analyze_operation
from app.code.repository import load_index
from app.code.translation import CandidateRejected, count_wrapper, evidence_rules, runtime_candidate
from oracle_mcp.policy.validator import SelectPolicy

FILES = {
    "src/main/java/demo/api/Paths.java": """
package demo.api;
public final class Paths {
    public static final String UNITS = "/units";
}
""",
    "src/main/java/demo/api/UnitController.java": """
package demo.api;
@RestController
@RequestMapping("/catalog")
public class UnitController {
    private final UnitService unitService;
    @GetMapping(Paths.UNITS)
    public Page<UnitDto> list(@RequestParam Long regionId, Pageable pageable) {
        return unitService.listUnits(regionId, pageable);
    }
    @GetMapping("/legacy-units")
    public List<UnitDto> legacy(@RequestParam Long regionId) {
        return unitService.legacyUnits(regionId);
    }
    @GetMapping("/active-teams")
    public List<TeamDto> teams(@RequestParam Long regionId) {
        return unitService.teams(regionId);
    }
}
""",
    "src/main/java/demo/service/UnitService.java": """
package demo.service;
public interface UnitService {
    Page<UnitDto> listUnits(Long regionId, Pageable pageable);
    List<UnitDto> legacyUnits(Long regionId);
    List<TeamDto> teams(Long regionId);
}
""",
    "src/main/java/demo/service/UnitServiceImpl.java": """
package demo.service;
@Service
public class UnitServiceImpl implements UnitService {
    private final UnitRepository unitRepository;
    private final UnitMapper unitMapper;
    private final TeamRepository teamRepository;
    @Override
    public Page<UnitDto> listUnits(Long regionId, Pageable pageable) {
        return unitRepository.findActiveUnits(regionId, pageable).map(this::toDto);
    }
    @Override
    public List<UnitDto> legacyUnits(Long regionId) {
        return unitMapper.selectLegacy(regionId);
    }
    @Override
    public List<TeamDto> teams(Long regionId) {
        return teamRepository.findByRegionIdAndDeletedAtIsNullAndActiveTrue(regionId);
    }
}
""",
    "src/main/java/demo/repo/UnitRepository.java": """
package demo.repo;
public interface UnitRepository extends JpaRepository<UnitEntity, Long> {
    @Query(value = "SELECT u.* FROM ORG_UNIT u " +
                   "WHERE u.REGION_ID = :regionId AND u.STATUS = 'A' ORDER BY u.NAME",
           nativeQuery = true)
    Page<UnitEntity> findActiveUnits(@Param("regionId") Long regionId, Pageable pageable);
}
""",
    "src/main/java/demo/repo/TeamRepository.java": """
package demo.repo;
public interface TeamRepository extends JpaRepository<TeamEntity, Long> {
    List<TeamEntity> findByRegionIdAndDeletedAtIsNullAndActiveTrue(Long regionId);
}
""",
    "src/main/java/demo/repo/TeamEntity.java": """
package demo.repo;
@Entity
@Table(name = "ORG_TEAM", schema = "HR")
@SQLRestriction("IS_ARCHIVED = 0")
public class TeamEntity {
    @Id
    @Column(name = "TEAM_ID")
    private Long id;
    @Column(name = "REGION_REF")
    private Long regionId;
    private LocalDateTime deletedAt;
    @Column(name = "IS_ACTIVE")
    private Boolean active;
    @Transient
    private String label;
}
""",
    "src/main/java/demo/repo/UnitMapper.java": """
package demo.repo;
@Mapper
public interface UnitMapper {
    List<UnitDto> selectLegacy(@Param("regionId") Long regionId);
}
""",
    "src/main/resources/mappers/UnitMapper.xml": """
<mapper namespace="demo.repo.UnitMapper">
  <select id="selectLegacy" resultType="UnitDto">
    SELECT * FROM LEGACY_UNIT WHERE REGION_ID = #{regionId}
    <if test="x != null"> AND X = #{x} </if>
  </select>
</mapper>
""",
}


@pytest.fixture
def index(tmp_path: Path):
    for relative, content in FILES.items():
        target = tmp_path / "repo" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return load_index([str(tmp_path / "repo")])


def test_route_with_class_prefix_and_constant_reaches_native_query(index):
    evidence = analyze_operation(index, "/api/v1/catalog/units", "GET")
    assert evidence.route.symbol == "UnitController.list"
    symbols = [location.symbol for location in evidence.chain]
    assert "UnitServiceImpl.listUnits" in symbols and "UnitRepository.findActiveUnits" in symbols
    native = next(query for query in evidence.queries if query.kind == "native")
    assert "FROM ORG_UNIT u" in native.text and "STATUS = 'A'" in native.text
    assert native.location.file.endswith("UnitRepository.java")


def test_mybatis_xml_select_is_found_and_marked_dynamic(index):
    evidence = analyze_operation(index, "/catalog/legacy-units", "GET")
    mybatis = next(query for query in evidence.queries if query.kind == "mybatis")
    assert "FROM LEGACY_UNIT" in mybatis.text and mybatis.dynamic


def test_derived_query_maps_properties_to_entity_columns(index):
    evidence = analyze_operation(index, "/catalog/active-teams", "GET")
    derived = evidence.derived_queries[0]
    assert [(condition.property, condition.operator, condition.column) for condition in derived.conditions] == [
        ("regionId", "=", "REGION_REF"),
        ("deletedAt", "IS NULL", "DELETED_AT"),
        ("active", "= TRUE", "IS_ACTIVE"),
    ]
    entity = evidence.entities[0]
    assert (entity.table, entity.schema_name, entity.restriction) == ("ORG_TEAM", "HR", "IS_ARCHIVED = 0")
    assert "label" not in entity.columns and entity.inferred_columns == ["deletedAt"]


def test_unknown_endpoint_reports_warning(index):
    evidence = analyze_operation(index, "/nothing/here", "GET")
    assert evidence.route is None and evidence.warnings


def test_native_query_becomes_qualified_count_with_api_binds(index):
    evidence = analyze_operation(index, "/catalog/units", "GET")
    native = next(query for query in evidence.queries if query.kind == "native")
    candidate = count_wrapper(native, {"regionId": "7"}, {"ORG_UNIT": "APP"})
    assert candidate.sql.startswith("SELECT COUNT(*) AS RESULT_VALUE FROM (SELECT")
    assert "APP.ORG_UNIT" in candidate.sql and "ORDER BY" not in candidate.sql.upper()
    assert candidate.binds == {"regionId": 7}
    assert SelectPolicy(["APP"]).validate(candidate.sql, candidate.binds).ok


def test_mybatis_conditional_blocks_follow_api_parameters(index):
    evidence = analyze_operation(index, "/catalog/legacy-units", "GET")
    mybatis = next(query for query in evidence.queries if query.kind == "mybatis")
    candidate = count_wrapper(mybatis, {"regionId": "3"}, {"LEGACY_UNIT": "APP"})
    assert "X =" not in candidate.sql and candidate.binds == {"regionId": 3}
    assert any("Koşullu SQL" in note for note in candidate.notes)


def test_unsafe_or_unresolvable_queries_are_rejected(index):
    evidence = analyze_operation(index, "/catalog/units", "GET")
    native = next(query for query in evidence.queries if query.kind == "native")
    with pytest.raises(CandidateRejected):
        count_wrapper(native, {"regionId": "7"}, {})
    unsafe = native.model_copy(update={"text": "SELECT * FROM ORG_UNIT WHERE NAME = '${name}'"})
    with pytest.raises(CandidateRejected):
        count_wrapper(unsafe, {}, {"ORG_UNIT": "APP"})


def test_evidence_becomes_rules_with_file_line_sources(index):
    rules = evidence_rules(analyze_operation(index, "/catalog/active-teams", "GET"))
    entity_rule = next(rule for rule in rules if "entity TeamEntity" in rule.text)
    assert "IS_ARCHIVED = 0" in entity_rule.text
    assert entity_rule.sources[0].source_filename.endswith("TeamEntity.java")
    assert entity_rule.sources[0].chunk_id.startswith("code:")


def test_class_annotation_without_arguments_does_not_steal_method_path(tmp_path):
    source = tmp_path / "repo" / "Controller.java"
    source.parent.mkdir(parents=True)
    source.write_text('@RestController\npublic class C {\n    @GetMapping("/orders")\n    public List<X> list() {\n        return null;\n    }\n}\n', encoding="utf-8")
    evidence = analyze_operation(load_index([str(source.parent)]), "/orders", "GET")
    assert evidence.route.symbol == "C.list"


def test_runtime_hibernate_query_drops_paging_and_maps_binds():
    raw = "select o1_0.ID,o1_0.STATUS from ORDERS o1_0 where o1_0.CUSTOMER_ID=? and o1_0.STATUS='A' order by o1_0.ID offset ? rows fetch first ? rows only"
    sql, binds, notes = runtime_candidate(raw, ["7"], {"ORDERS": "APP"})
    assert sql.startswith("SELECT COUNT(*) AS RESULT_VALUE FROM (SELECT") and "APP.ORDERS" in sql
    assert "OFFSET" not in sql.upper() and "FETCH" not in sql.upper()
    assert binds == {"p1": 7} and "sezgisel" in notes[0]


def test_runtime_count_query_is_used_as_is_with_captured_binds():
    sql, binds, notes = runtime_candidate("select count(o1_0.ID) from ORDERS o1_0 where o1_0.CUSTOMER_ID=:1", ["99"], {"ORDERS": "APP"}, ["7"])
    assert sql.upper().startswith("SELECT COUNT(") and "CF_SOURCE" not in sql
    assert binds == {"p1": 7} and "V$SQL_BIND_CAPTURE" in notes[0]


def test_runtime_candidate_rejects_unmappable_parameters():
    with pytest.raises(CandidateRejected):
        runtime_candidate("select ID from ORDERS where A=? and B=?", ["1"], {"ORDERS": "APP"})
