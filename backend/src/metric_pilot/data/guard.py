"""DuckDB SQL 的默认拒绝 AST 白名单，不执行原始未验证文本。"""

import math
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from metric_pilot.data.common import DataError, canonical_bytes

GUARD_VERSION = "duckdb_allowlist_v1"
FUNCTIONS = {
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "COALESCE",
    "NULLIF",
    "ROUND",
    "ABS",
    "FLOOR",
    "CEIL",
    "LOWER",
    "UPPER",
    "LENGTH",
    "SUBSTRING",
    "CAST",
    "TRY_CAST",
    "CASE",
    "IF",
    "DATE_TRUNC",
    "TIMESTAMP_TRUNC",
    "EXTRACT",
}
NODES = {
    "select",
    "union",
    "intersect",
    "except",
    "with",
    "cte",
    "from",
    "table",
    "tablealias",
    "column",
    "identifier",
    "literal",
    "null",
    "boolean",
    "star",
    "alias",
    "where",
    "group",
    "having",
    "order",
    "ordered",
    "limit",
    "offset",
    "join",
    "subquery",
    "paren",
    "distinct",
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "and",
    "or",
    "not",
    "is",
    "in",
    "between",
    "add",
    "sub",
    "mul",
    "div",
    "mod",
    "neg",
    "datatype",
    "datatypeparam",
    "var",
    "placeholder",
    "tuple",
    "filter",
    "window",
    "windowspec",
    "interval",
}


@dataclass(frozen=True)
class GuardedQuery:
    """
    作用：保存完整校验后的执行计划信息
    输入：原 SQL、规范 SQL、受限 SQL、引用和参数
    输出：不可变校验结果
    """

    original_sql: str
    normalized_sql: str
    limited_sql: str
    tables: list[str]
    functions: list[str]
    parameter_keys: list[str]
    ordered: bool


def validate_sql(
    sql: str, parameters: dict, allowed_tables: set[str], max_rows: int
) -> GuardedQuery:
    """
    作用：验证单条查询的全部节点、函数、作用域与标量绑定
    输入：SQL、绑定值、服务端公开表集合与结果上限
    输出：受保护查询；非法输入在连接数据库前失败
    """
    if not isinstance(sql, str) or not sql.strip() or len(sql.encode()) > 65536:
        raise DataError("INVALID_SQL_SIZE")
    if (
        not isinstance(parameters, dict)
        or len(parameters) > 100
        or any(
            not isinstance(k, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            or (v is not None and type(v) not in (str, int, float, bool))
            or (isinstance(v, float) and not math.isfinite(v))
            or (isinstance(v, int) and not -(2**63) <= v < 2**63)
            or (isinstance(v, str) and len(v.encode()) > 65536)
            for k, v in parameters.items()
        )
    ):
        raise DataError("INVALID_PARAMETERS")
    if len(canonical_bytes(parameters)) > 65536:
        raise DataError("INVALID_PARAMETERS")
    try:
        roots = sqlglot.parse(sql, read="duckdb", error_level=sqlglot.ErrorLevel.RAISE)
        if len(roots) != 1 or not isinstance(
            roots[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
        ):
            raise DataError("ONLY_ONE_QUERY_ALLOWED")
        tree = roots[0]
        nodes = list(tree.walk())
        if len(nodes) > 5000:
            raise DataError("SQL_TOO_COMPLEX")
        functions = set()
        for node in nodes:
            if isinstance(node, exp.Func) and node.key not in NODES:
                function = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
                if function not in FUNCTIONS:
                    raise DataError("FUNCTION_NOT_ALLOWED", {"function": function})
                functions.add(function)
            elif node.key not in NODES:
                raise DataError("SQL_NODE_NOT_ALLOWED", {"node": node.key})
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise DataError("RECURSIVE_CTE_NOT_ALLOWED")
            if isinstance(node, exp.Table):
                if not isinstance(node.this, exp.Identifier) or node.db or node.catalog:
                    raise DataError("TABLE_REFERENCE_NOT_ALLOWED")
            if isinstance(node, exp.Column) and (node.db or node.catalog):
                raise DataError("QUALIFIED_COLUMN_NOT_ALLOWED")
            if isinstance(node, exp.DataType):
                if str(node.this).split(".")[-1] not in {
                    "BIGINT",
                    "INT",
                    "SMALLINT",
                    "TINYINT",
                    "DOUBLE",
                    "FLOAT",
                    "DECIMAL",
                    "VARCHAR",
                    "TEXT",
                    "BOOLEAN",
                    "DATE",
                    "TIMESTAMP",
                }:
                    raise DataError("CAST_TYPE_NOT_ALLOWED")
        # selected_sources 区分 CTE 别名与真实物理表，不按字符串猜测表名。
        physical = set()
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table) or source.name.lower() not in allowed_tables:
                    raise DataError("TABLE_NOT_ALLOWED")
                physical.add(source.name.lower())
        keys = set()
        for node in tree.find_all(exp.Placeholder):
            if not node.name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.name):
                raise DataError("NAMED_PARAMETERS_ONLY")
            keys.add(node.name)
        if keys != set(parameters):
            raise DataError("PARAMETER_MISMATCH", {"expected": sorted(keys)})
        # 外层限流不改写内部聚合，也不扩大用户原有 LIMIT。
        limited = exp.select("*").from_(tree.copy().subquery("_mp_result")).limit(max_rows + 1)
        return GuardedQuery(
            sql,
            tree.sql(dialect="duckdb"),
            limited.sql(dialect="duckdb"),
            sorted(physical),
            sorted(functions),
            sorted(keys),
            bool(tree.args.get("order")),
        )
    except DataError:
        raise
    except (sqlglot.errors.SqlglotError, ValueError, TypeError, RecursionError):
        raise DataError("SQL_PARSE_ERROR") from None
