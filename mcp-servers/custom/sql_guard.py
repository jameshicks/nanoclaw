"""sqlglot-based validator for run_readonly_sql. Read-only DuckDB connection is the real guard;
this exists for nicer error messages and to stop obvious catalog-snooping before the DB sees it."""

import sqlglot
from sqlglot import exp

_BANNED_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Copy,
    exp.Pragma,
    exp.Use,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Merge,
    exp.TruncateTable,
)

_BANNED_TABLE_PREFIXES = ("duckdb_",)
_BANNED_SCHEMAS = {"information_schema", "pg_catalog", "system", "temp"}


def validate(query: str) -> None:
    trees = sqlglot.parse(query, read="duckdb")
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        raise ValueError("exactly one SQL statement allowed")
    tree = trees[0]

    root_select = tree.find(exp.Select)
    if root_select is None:
        raise ValueError("only SELECT (optionally wrapped in WITH) allowed")
    if not isinstance(tree, (exp.Select, exp.Subquery, exp.With)) and tree.key != "select":
        raise ValueError(f"only SELECT allowed, got {type(tree).__name__}")

    for node in tree.walk():
        if isinstance(node, _BANNED_NODE_TYPES):
            raise ValueError(f"banned statement type: {type(node).__name__}")

    for tbl in tree.find_all(exp.Table):
        tbl_name = (tbl.name or "").lower()
        schema = (tbl.db or "").lower()
        if any(tbl_name.startswith(p) for p in _BANNED_TABLE_PREFIXES):
            raise ValueError(f"banned table reference: {tbl_name}")
        if schema in _BANNED_SCHEMAS or tbl_name in _BANNED_SCHEMAS:
            raise ValueError(f"banned schema: {schema or tbl_name}")

    # Table-returning functions (e.g. `FROM duckdb_functions()`) parse as Anonymous,
    # not as Table. Also block any duckdb_* function invocation.
    for fn in tree.find_all(exp.Anonymous):
        fn_name = (fn.name or "").lower()
        if any(fn_name.startswith(p) for p in _BANNED_TABLE_PREFIXES):
            raise ValueError(f"banned function: {fn_name}")
        if fn_name in {"attach", "detach", "load_aws_credentials"}:
            raise ValueError(f"banned function: {fn_name}")
