"""筛选条件：同一份条件同时生成下推 SQL 与 Python 谓词。

- 下推模式：SQL 片段 + 参数，交给源数据库执行，减少传回应用层的数据。
- 整表对照模式：等价的 Python 谓词，在应用层对全表行求值。
两种路径必须语义一致，对照实验依赖这一点。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SQL_OPS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
    "in": "IN",
    "like": "LIKE",
    "isnull": "IS NULL",
    "notnull": "IS NOT NULL",
}


def coerce_value(value, decl_type: str):
    """按列声明类型把 JSON 传来的字符串值转换为 Python 值。"""
    if value is None:
        return None
    t = (decl_type or "TEXT").upper()
    if "INT" in t:
        return int(value)
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return float(value)
    return str(value)


def quote_ident(name: str) -> str:
    """SQL 标识符引用，防止注入与关键字冲突。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法标识符: {name!r}")
    return f'"{name}"'


@dataclass(frozen=True)
class Filter:
    source: str
    column: str
    op: str
    value: object  # 标量；in 为 list；isnull/notnull 为 None

    @classmethod
    def from_dict(cls, d: dict, col_types: dict[str, dict[str, str]]):
        op = d["op"]
        if op not in SQL_OPS:
            raise ValueError(f"不支持的操作符: {op}")
        source = d["source"]
        column = d["column"]
        decl = col_types[source][column]
        raw = d.get("value")
        if op in ("isnull", "notnull"):
            value = None
        elif op == "in":
            if isinstance(raw, str):
                raw = [v.strip() for v in raw.split(",") if v.strip()]
            if not isinstance(raw, list) or not raw:
                raise ValueError("in 操作需要非空列表值")
            value = [coerce_value(v, decl) for v in raw]
        else:
            if raw is None:
                raise ValueError(f"{op} 操作需要值")
            value = coerce_value(raw, decl)
        return cls(source=source, column=column, op=op, value=value)


def _like_regex(pattern: str) -> re.Pattern:
    """把 SQL LIKE 模式（% 与 _）翻译为 Python 正则。"""
    out = ["^"]
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    # SQLite LIKE 默认对 ASCII 大小写不敏感
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def split_by_source(filters: list[Filter]) -> dict[str, list[Filter]]:
    grouped: dict[str, list[Filter]] = {}
    for f in filters:
        grouped.setdefault(f.source, []).append(f)
    return grouped


def build_sql_where(filters: list[Filter]) -> tuple[str, list]:
    """生成 WHERE 片段（不含 WHERE 关键字）与绑定参数。"""
    clauses: list[str] = []
    params: list = []
    for f in filters:
        col = quote_ident(f.column)
        if f.op in ("isnull", "notnull"):
            clauses.append(f"{col} {SQL_OPS[f.op]}")
        elif f.op == "in":
            placeholders = ", ".join("?" for _ in f.value)
            clauses.append(f"{col} IN ({placeholders})")
            params.extend(f.value)
        else:
            clauses.append(f"{col} {SQL_OPS[f.op]} ?")
            params.append(f.value)
    return " AND ".join(clauses), params


def build_py_predicate(filters: list[Filter]):
    """生成应用层谓词，语义与 build_sql_where 保持一致。"""
    testers = []
    for f in filters:
        col, op, val = f.column, f.op, f.value

        def tester(row, col=col, op=op, val=val):
            cell = row.get(col)
            if op == "isnull":
                return cell is None
            if op == "notnull":
                return cell is not None
            if cell is None:
                # SQL 三值逻辑：NULL 参与比较结果为 unknown（即不选中）
                return False
            if op == "eq":
                return cell == val
            if op == "ne":
                return cell != val
            if op == "gt":
                return cell > val
            if op == "ge":
                return cell >= val
            if op == "lt":
                return cell < val
            if op == "le":
                return cell <= val
            if op == "in":
                return cell in val
            if op == "like":
                return _like_regex(val).match(str(cell)) is not None
            raise ValueError(f"不支持的操作符: {op}")

        testers.append(tester)

    def predicate(row: dict) -> bool:
        return all(t(row) for t in testers)

    return predicate
