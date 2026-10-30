import pytest

from app.core.models.common import ErrorCode
from oracle_mcp.policy.validator import ObjectCatalog, SelectPolicy

CATALOG = ObjectCatalog(
    columns_by_table={
        "APP.ORDERS": {"ID", "CUSTOMER_ID", "STATUS", "DELETED", "AMOUNT", "CREATED_AT"},
        "APP.CUSTOMERS": {"ID", "NAME", "ACTIVE"},
        "APP.ORDER_LINES": {"ID", "ORDER_ID", "QTY"},
        "OTHER.SECRET": {"ID"},
    }
)


@pytest.fixture
def policy() -> SelectPolicy:
    return SelectPolicy(["APP"], CATALOG)


@pytest.mark.parametrize(
    ("sql", "binds"),
    [
        ("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS = :status", {"status": "A"}),
        ("SELECT COUNT(DISTINCT o.ID) FROM APP.ORDERS o INNER JOIN APP.CUSTOMERS c ON c.ID = o.CUSTOMER_ID WHERE c.ACTIVE = :a", {"a": 1}),
        ("SELECT o.ID FROM APP.ORDERS o LEFT JOIN APP.ORDER_LINES l ON l.ORDER_ID = o.ID", {}),
        ("SELECT o.ID FROM APP.ORDERS o RIGHT JOIN APP.ORDER_LINES l ON l.ORDER_ID = o.ID", {}),
        ("SELECT o.ID FROM APP.ORDERS o FULL OUTER JOIN APP.ORDER_LINES l ON l.ORDER_ID = o.ID", {}),
        ("SELECT o.ID FROM APP.ORDERS o CROSS JOIN APP.CUSTOMERS c", {}),
        ("SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON c.ID = o.CUSTOMER_ID JOIN APP.ORDER_LINES l ON l.ORDER_ID = o.ID", {}),
        ("SELECT COUNT(*) FROM APP.ORDERS o WHERE (o.STATUS = :s1 OR o.STATUS = :s2) AND o.DELETED IS NULL", {"s1": "A", "s2": "B"}),
        ("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS IN (:a, :b) AND o.STATUS LIKE :c", {"a": 1, "b": 2, "c": "A%"}),
        ("SELECT COUNT(*) FROM APP.ORDERS o WHERE EXISTS (SELECT 1 FROM APP.ORDER_LINES l WHERE l.ORDER_ID = o.ID)", {}),
        ("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.CUSTOMER_ID IN (SELECT c.ID FROM APP.CUSTOMERS c WHERE c.ACTIVE = 1)", {}),
        ("WITH active_orders AS (SELECT o.ID FROM APP.ORDERS o WHERE o.DELETED = 0) SELECT COUNT(*) FROM active_orders", {}),
        ("SELECT DISTINCT o.STATUS FROM APP.ORDERS o", {}),
        ("SELECT SUM(o.AMOUNT), AVG(o.AMOUNT), MIN(o.AMOUNT), MAX(o.AMOUNT) FROM APP.ORDERS o", {}),
        ("SELECT o.STATUS, COUNT(*) FROM APP.ORDERS o GROUP BY o.STATUS HAVING COUNT(*) > 1 ORDER BY o.STATUS", {}),
        ("SELECT o.ID FROM APP.ORDERS o UNION SELECT c.ID FROM APP.CUSTOMERS c", {}),
        ("SELECT o.ID FROM APP.ORDERS o UNION ALL SELECT c.ID FROM APP.CUSTOMERS c", {}),
        ("SELECT o.ID FROM APP.ORDERS o FETCH FIRST 10 ROWS ONLY", {}),
        ("SELECT NVL(o.AMOUNT, 0), TRUNC(o.CREATED_AT), TO_CHAR(o.CREATED_AT, 'YYYY') FROM APP.ORDERS o", {}),
        ("SELECT 1 FROM DUAL", {}),
    ],
)
def test_supported_select_shapes_pass(policy, sql, binds):
    result = policy.validate(sql, binds)
    assert result.ok, result.message


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DELETE FROM APP.ORDERS", ErrorCode.POLICY_VIOLATION),
        ("UPDATE APP.ORDERS SET STATUS = 'X'", ErrorCode.POLICY_VIOLATION),
        ("INSERT INTO APP.ORDERS (ID) VALUES (1)", ErrorCode.POLICY_VIOLATION),
        ("MERGE INTO APP.ORDERS o USING APP.CUSTOMERS c ON (o.ID = c.ID) WHEN MATCHED THEN UPDATE SET o.STATUS = 'X'", ErrorCode.POLICY_VIOLATION),
        ("DROP TABLE APP.ORDERS", ErrorCode.POLICY_VIOLATION),
        ("CREATE TABLE APP.X (ID NUMBER)", ErrorCode.POLICY_VIOLATION),
        ("TRUNCATE TABLE APP.ORDERS", ErrorCode.POLICY_VIOLATION),
        ("GRANT SELECT ON APP.ORDERS TO PUBLIC", ErrorCode.POLICY_VIOLATION),
        ("SELECT 1 FROM DUAL; DELETE FROM APP.ORDERS", ErrorCode.POLICY_VIOLATION),
        ("SELECT * FROM APP.ORDERS FOR UPDATE", ErrorCode.POLICY_VIOLATION),
        ("SELECT o.ID INTO v_id FROM APP.ORDERS o", ErrorCode.POLICY_VIOLATION),
        ("SELECT * FROM APP.ORDERS@remote_link", ErrorCode.POLICY_VIOLATION),
        ("WITH FUNCTION f(x NUMBER) RETURN NUMBER IS BEGIN RETURN x; END; SELECT f(1) FROM DUAL", ErrorCode.UNSUPPORTED_QUERY),
        ("BEGIN DELETE FROM APP.ORDERS; END;", ErrorCode.UNSUPPORTED_QUERY),
        ("SELECT dbms_random.value() FROM DUAL", ErrorCode.POLICY_VIOLATION),
        ("SELECT APP.side_effect_fn(o.ID) FROM APP.ORDERS o", ErrorCode.POLICY_VIOLATION),
        ("SELECT my_udf(o.ID) FROM APP.ORDERS o", ErrorCode.POLICY_VIOLATION),
        ("SELECT * FROM OTHER.SECRET", ErrorCode.POLICY_VIOLATION),
        ("SELECT * FROM ORDERS", ErrorCode.POLICY_VIOLATION),
        ("SELECT * FROM APP.UNKNOWN_TABLE", ErrorCode.UNKNOWN_OBJECT),
        ("SELECT o.NOT_A_COLUMN FROM APP.ORDERS o", ErrorCode.UNKNOWN_OBJECT),
        ("SELECT o.ID FROM APP.ORDERS o WHERE o.ID = ?", ErrorCode.POLICY_VIOLATION),
        ("", ErrorCode.UNSUPPORTED_QUERY),
    ],
)
def test_dangerous_or_unknown_sql_is_rejected(policy, sql, code):
    result = policy.validate(sql, {})
    assert not result.ok
    assert result.error_code is code, result.message


def test_cte_alias_cannot_shadow_forbidden_schema(policy):
    sql = "WITH ORDERS AS (SELECT s.ID FROM OTHER.SECRET s) SELECT COUNT(*) FROM ORDERS"
    result = policy.validate(sql, {})
    assert not result.ok
    assert result.error_code is ErrorCode.POLICY_VIOLATION


def test_union_branches_are_checked(policy):
    result = policy.validate("SELECT o.ID FROM APP.ORDERS o UNION SELECT s.ID FROM OTHER.SECRET s", {})
    assert not result.ok


def test_forbidden_node_inside_cte_is_rejected(policy):
    result = policy.validate("WITH x AS (SELECT o.ID FROM APP.ORDERS o FOR UPDATE) SELECT COUNT(*) FROM x", {})
    assert not result.ok


def test_bind_values_must_match_placeholders(policy):
    assert not policy.validate("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS = :status", {}).ok
    assert not policy.validate("SELECT COUNT(*) FROM APP.ORDERS o", {"unused": 1}).ok
    result = policy.validate("SELECT COUNT(*) FROM APP.ORDERS o WHERE o.STATUS = :status", {"status": "A"})
    assert result.bind_names == ["status"]


def test_referenced_tables_are_reported(policy):
    result = policy.validate("SELECT COUNT(*) FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON c.ID = o.CUSTOMER_ID", {})
    assert result.tables == ["APP.CUSTOMERS", "APP.ORDERS"]
