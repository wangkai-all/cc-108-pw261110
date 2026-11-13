"""跨库查询执行引擎。

两种执行方式：
1. pushdown（键下推 + 分批查询）：
   能在源端完成的筛选先下推；先取符合条件的客户键（去重、去 NULL），
   再按批次以 IN 列表查询订单，批次大小受控，取消后停止剩余请求。
2. full_scan（整表对照）：
   分别把两张表整表读回应用层，用等价的 Python 谓词筛选后做同样的哈希连接。

连接语义（明确、可测试）：
- 内连接：只保留两源都匹配上的明细行；
- 客户键 NULL：不参与连接，单独计数，绝不静默丢弃；
- 客户键重复：订单与每个匹配客户行各产生一条明细（SQL JOIN 乘法语义），
  订单侧 IN 查询只发送去重后的键以减少传输，连接在应用层还原完整行；
- 同一客户多笔订单：全部保留，不做任何"为了少传数据"的预聚合或截断；
- 任一批次失败：整次执行失败，失败批次不当作空结果，不形成可下载结果。
"""
from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass, field

from .predicates import (
    Filter, build_py_predicate, build_sql_where, quote_ident,
    split_by_source,
)
from .schema import (
    CUSTOMERS_TABLE, DEFAULT_BATCH_SIZE, DETAIL_PREVIEW_LIMIT, FOREIGN_KEY,
    JOIN_KEY, ORDERS_TABLE, SOURCES,
)
from .sources import (
    BatchFailed, QueryCancelled, SourceSnapshot, close_snapshots, open_snapshots,
)

CUSTOMER_SRC = "customers"
ORDER_SRC = "orders"


@dataclass
class QueryConfig:
    select_columns: dict[str, list[str]]
    filters: list[Filter]
    join_key: str = JOIN_KEY
    batch_size: int = DEFAULT_BATCH_SIZE
    fail_batch_no: dict[str, int] = field(default_factory=dict)  # source -> 第几批注入失败（1 起）


@dataclass
class RunRequest:
    mode: str                        # pushdown | full_scan | compare
    config: QueryConfig
    injected_delays_ms: dict[str, int] = field(default_factory=dict)
    timeouts_ms: dict[str, int] = field(default_factory=dict)


class EngineError(Exception):
    pass


# --------------------------------------------------------------------------
# 配置校验与列处理
# --------------------------------------------------------------------------

def _ensure_required_columns(source: str, selected: list[str], required: list[str]) -> list[str]:
    """保证连接键等内部必需列被取回（不额外展示），同时去重保序。"""
    out = list(selected)
    for col in required:
        if col not in out and col in SOURCES[source]["table"].col_types:
            out.append(col)
    seen = set()
    deduped = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _columns_for_source(source: str, config: QueryConfig) -> list[str]:
    """该源需要取回的列：用户选择列 + 连接键 + 该源全部筛选列。

    整表对照模式在应用层执行谓词，因此筛选列也必须实际取回；
    下推模式多取这些列不影响正确性，且保证两条路径读取口径一致。
    """
    required = [config.join_key]
    if source == ORDER_SRC:
        required += ["order_id", "amount"]
    required += [f.column for f in config.filters if f.source == source]
    return _ensure_required_columns(
        source, config.select_columns.get(source, []), required
    )


def validate_config(config: QueryConfig) -> None:
    if config.join_key not in CUSTOMERS_TABLE.col_types:
        raise EngineError(f"客户表不存在关联键列: {config.join_key}")
    if config.join_key not in ORDERS_TABLE.col_types:
        raise EngineError(f"订单表不存在关联键列: {config.join_key}")
    if config.batch_size < 1:
        raise EngineError("批次大小必须 >= 1")
    known = {s: set(SOURCES[s]["table"].col_types) for s in SOURCES}
    for source, cols in config.select_columns.items():
        if source not in known:
            raise EngineError(f"未知数据源: {source}")
        for c in cols:
            if c not in known[source]:
                raise EngineError(f"{source} 不存在列: {c}")
    grouped = split_by_source(config.filters)
    for source, fl in grouped.items():
        if source not in known:
            raise EngineError(f"筛选条件引用了未知数据源: {source}")
        for f in fl:
            if f.column not in known[source]:
                raise EngineError(f"{source} 不存在筛选列: {f.column}")


def _select_sql(table_name: str, columns: list[str]) -> str:
    cols = ", ".join(quote_ident(c) for c in columns)
    return f"SELECT {cols} FROM {quote_ident(table_name)}"


# --------------------------------------------------------------------------
# 哈希连接（两条执行路径共用，保证语义一致）
# --------------------------------------------------------------------------

def hash_join(customers: list[dict], orders: list[dict], key: str,
              null_order_override: int | None = None,
              keyed_order_total_override: int | None = None
              ) -> tuple[list[dict], dict]:
    """应用层内连接，语义等价于 SQL INNER JOIN ON c.key = o.key。

    返回 (明细行, 语义统计)。明细行只保留实际发生的匹配。
    下推模式下部分订单行不会传回应用层，可通过 override 传入源端 COUNT 的
    真实计数，避免把"没拉回来"误报为 0。
    """
    index: dict = {}
    null_customer_rows = 0
    duplicate_key_groups = 0
    for crow in customers:
        k = crow.get(key)
        if k is None:
            null_customer_rows += 1
            continue  # SQL: NULL = NULL 不为真，NULL 键永不匹配
        bucket = index.get(k)
        if bucket is None:
            index[k] = [crow]
        else:
            if len(bucket) == 1:
                duplicate_key_groups += 1
            bucket.append(crow)

    detail = []
    null_order_rows = 0
    unmatched_order_rows = 0
    matched_order_rows = 0
    for orow in orders:
        k = orow.get(key)
        if k is None:
            null_order_rows += 1
            continue
        bucket = index.get(k)
        if not bucket:
            unmatched_order_rows += 1
            continue
        matched_order_rows += 1
        for crow in bucket:  # 重复客户键 → 每个客户行各产出一条明细
            merged = {}
            merged.update({f"c_{c}": v for c, v in crow.items()})
            merged.update({f"o_{c}": v for c, v in orow.items()})
            detail.append(merged)

    matched_keys = len({r.get(key) for r in customers if r.get(key) is not None}
                       & {r.get(key) for r in orders if r.get(key) is not None})
    # 下推模式：应用层看不到未拉回的 NULL/孤儿行，用源端真实 COUNT 覆盖
    effective_null_orders = (null_order_override if null_order_override is not None
                             else null_order_rows)
    if keyed_order_total_override is not None:
        effective_unmatched = keyed_order_total_override - matched_order_rows
    else:
        effective_unmatched = unmatched_order_rows
    semantics = {
        "join_type": "INNER JOIN",
        "join_key": key,
        "customer_rows_in": len(customers),
        "order_rows_in": len(orders),
        "distinct_customer_keys": len(index),
        "duplicate_customer_key_groups": duplicate_key_groups,
        "null_customer_key_rows_excluded": null_customer_rows,
        "null_order_key_rows_excluded": effective_null_orders,
        "unmatched_order_rows": effective_unmatched,
        "matched_order_rows": matched_order_rows,
        "matched_distinct_keys": matched_keys,
        "detail_rows": len(detail),
    }
    # 确定性排序：两条执行路径的行到达顺序不同（分批 vs 全表），
    # 统一按 关联键、订单号、客户行内容 排序后才能逐行比对
    detail.sort(key=lambda r: (
        str(r.get("o_" + key) or ""),
        str(r.get("o_order_id") or ""),
        json.dumps(r, ensure_ascii=False, sort_keys=True, default=str),
    ))
    return detail, semantics


def aggregate(detail: list[dict]) -> dict:
    """对最终明细做汇总；全部由真实明细计算，无预设数字。"""
    total_amount = 0.0
    amount_seen = 0
    customer_keys = set()
    for r in detail:
        amt = r.get("o_amount")
        if amt is not None:
            total_amount += float(amt)
            amount_seen += 1
        # 兼容连接前（o_ 前缀）与投影后（c_ 前缀的关联键）两种明细形态
        k = r.get("o_" + JOIN_KEY)
        if k is None:
            k = r.get("c_" + JOIN_KEY)
        if k is not None:
            customer_keys.add(k)
    return {
        "detail_row_count": len(detail),
        "distinct_customers_with_orders": len(customer_keys),
        "total_amount": round(total_amount, 2),
        "amount_values_counted": amount_seen,
    }


def _project_detail(detail: list[dict], config: QueryConfig) -> list[dict]:
    """按用户选择的字段裁剪明细；连接键始终保留以便核对。"""
    wanted = ["o_order_id", "c_" + config.join_key]
    for c in config.select_columns.get(CUSTOMER_SRC, []):
        wanted.append("c_" + c)
    for c in config.select_columns.get(ORDER_SRC, []):
        wanted.append("o_" + c)
    # 去重保序
    seen = set()
    wanted = [w for w in wanted if not (w in seen or seen.add(w))]
    return [{w: r.get(w) for w in wanted} for r in detail]


# --------------------------------------------------------------------------
# 执行计划（执行前生成的静态计划 + 执行后补充的实际统计）
# --------------------------------------------------------------------------

def _plan_pushdown(config: QueryConfig, grouped: dict[str, list[Filter]]) -> dict:
    c_where, c_params = build_sql_where(grouped.get(CUSTOMER_SRC, []))
    o_where, o_params = build_sql_where(grouped.get(ORDER_SRC, []))
    return {
        "strategy": "pushdown",
        "description": "筛选下推至源端；先取符合条件的客户键，再分批 IN 查询订单",
        "steps": [
            {
                "step": 1, "source": CUSTOMER_SRC,
                "action": "下推客户筛选并取回客户行（连接键必需）",
                "pushed_down_filters": [f"{f.column} {f.op}" for f in grouped.get(CUSTOMER_SRC, [])],
            },
            {
                "step": 2, "source": CUSTOMER_SRC,
                "action": "应用层对客户键去重、剔除 NULL，形成 IN 键集",
            },
            {
                "step": 3, "source": ORDER_SRC,
                "action": f"按批次（每批 {config.batch_size} 个键）下推订单筛选与 IN 键集查询；"
                          "取消即停止剩余批次",
                "pushed_down_filters": [f"{f.column} {f.op}" for f in grouped.get(ORDER_SRC, [])],
            },
            {"step": 4, "source": "app", "action": "应用层哈希内连接（重复键/NULL 按 SQL 语义处理）"},
            {"step": 5, "source": "app", "action": "字段裁剪与汇总"},
        ],
        "planned_sql": {
            CUSTOMER_SRC: {
                "sql": _select_sql(CUSTOMERS_TABLE.name, []) + (f" WHERE {c_where}" if c_where else ""),
                "params": c_params,
            },
            ORDER_SRC: {
                "sql_template": _select_sql(ORDERS_TABLE.name, [])
                               + (f" WHERE ({o_where}) AND " if o_where else " WHERE ")
                               + f'"{config.join_key}" IN ({{placeholders}})',
                "params_template": o_params,
            },
        },
    }


def _plan_full_scan(grouped: dict[str, list[Filter]]) -> dict:
    return {
        "strategy": "full_scan",
        "description": "整表对照：两表分别全量读回，筛选与连接全部在应用层完成",
        "steps": [
            {"step": 1, "source": CUSTOMER_SRC, "action": "整表读取 customers（无下推）"},
            {"step": 2, "source": ORDER_SRC, "action": "整表读取 orders（无下推）"},
            {"step": 3, "source": "app",
             "action": "应用层谓词筛选（与下推 SQL 语义等价的 Python 谓词）",
             "applied_filters": {s: [f"{f.column} {f.op}" for f in fl]
                                 for s, fl in grouped.items()}},
            {"step": 4, "source": "app", "action": "同一套哈希内连接逻辑"},
            {"step": 5, "source": "app", "action": "字段裁剪与汇总"},
        ],
    }


# --------------------------------------------------------------------------
# 执行路径一：键下推 + 分批
# --------------------------------------------------------------------------

def execute_pushdown(config: QueryConfig, snapshots: dict[str, SourceSnapshot]
                     ) -> dict:
    grouped = split_by_source(config.filters)
    plan = _plan_pushdown(config, grouped)
    c_snap = snapshots[CUSTOMER_SRC]
    o_snap = snapshots[ORDER_SRC]
    c_snap.stats.filters_pushed_down = [
        f"{f.column} {f.op}" for f in grouped.get(CUSTOMER_SRC, [])
    ]
    o_snap.stats.filters_pushed_down = [
        f"{f.column} {f.op}" for f in grouped.get(ORDER_SRC, [])
    ]

    # --- 步骤 1：客户筛选下推 ---
    c_cols = _columns_for_source(CUSTOMER_SRC, config)
    c_sql = _select_sql(CUSTOMERS_TABLE.name, c_cols)
    c_where, c_params = build_sql_where(grouped.get(CUSTOMER_SRC, []))
    if c_where:
        c_sql += f" WHERE {c_where}"
    # 执行计划：真实 EXPLAIN QUERY PLAN
    plan["explain_query_plan"] = {
        CUSTOMER_SRC: c_snap.explain(c_sql, c_params),
    }
    if config.fail_batch_no.get(CUSTOMER_SRC) == 1:
        # 客户侧只有一次查询，注入失败：显式失败，不当作空结果
        c_snap.stats.queries.append({
            "sql": c_sql, "params": c_params, "row_count": 0, "bytes": 0,
            "elapsed_ms": 0.0, "mode": "pushdown", "batch_no": 1,
            "injected_failure": True,
        })
        raise BatchFailed(f"{c_snap.label} 查询被注入失败（已停止，未作为空结果处理）")
    customers = c_snap.query(c_sql, c_params, mode="pushdown", batch_no=None)

    # --- 步骤 2：键集（去重、去 NULL，语义计数在 hash_join 内做）---
    distinct_keys = []
    seen = set()
    for row in customers:
        k = row.get(config.join_key)
        if k is None or k in seen:
            continue
        seen.add(k)
        distinct_keys.append(k)

    # --- 步骤 3：订单分批 IN 查询，订单自身筛选同时下推 ---
    o_cols = _columns_for_source(ORDER_SRC, config)
    o_base = _select_sql(ORDERS_TABLE.name, o_cols)
    o_where, o_where_params = build_sql_where(grouped.get(ORDER_SRC, []))
    orders: list[dict] = []
    batches: list[dict] = []
    fail_no = config.fail_batch_no.get(ORDER_SRC)
    total_batches = (len(distinct_keys) + config.batch_size - 1) // config.batch_size \
        if distinct_keys else 0

    # 语义统计的补数查询：IN 分批只会拉回匹配客户的订单，
    # NULL 键订单与孤儿订单不会进入连接层，这里以轻量 COUNT 取得真实计数
    # （只传回单行标量，不违背"少传数据"的目标；计数同样来自真实查询）。
    count_base = f'SELECT COUNT(*) AS cnt FROM {quote_ident(ORDERS_TABLE.name)}'
    meta_where, meta_params = build_sql_where(grouped.get(ORDER_SRC, []))
    meta_prefix = f"{count_base} WHERE {meta_where} AND " if meta_where else f"{count_base} WHERE "
    null_count = o_snap.query(
        meta_prefix + f"{quote_ident(config.join_key)} IS NULL",
        list(meta_params), mode="pushdown_meta", batch_no=None,
    )[0]["cnt"]
    keyed_total = o_snap.query(
        meta_prefix + f"{quote_ident(config.join_key)} IS NOT NULL",
        list(meta_params), mode="pushdown_meta", batch_no=None,
    )[0]["cnt"]

    if not distinct_keys:
        # 没有符合条件的客户键：不向订单库发送明细 IN 请求（真实的零明细请求）
        pass
    for idx in range(total_batches):
        batch_no = idx + 1
        chunk = distinct_keys[idx * config.batch_size:(idx + 1) * config.batch_size]
        placeholders = ", ".join("?" for _ in chunk)
        sql = f"{o_base} WHERE "
        params: list = []
        if o_where:
            sql += f"({o_where}) AND "
            params.extend(o_where_params)
        sql += f'{quote_ident(config.join_key)} IN ({placeholders})'
        params.extend(chunk)
        if batch_no == 1:
            plan["explain_query_plan"][ORDER_SRC] = o_snap.explain(sql, params)
        rows = o_snap.query(
            sql, params, mode="pushdown", batch_no=batch_no,
            fail_this_batch=(fail_no == batch_no),
        )
        orders.extend(rows)
        batches.append({"batch_no": batch_no, "keys": len(chunk),
                        "rows_returned": len(rows)})

    # --- 步骤 4/5：连接、裁剪、汇总 ---
    detail, semantics = hash_join(
        customers, orders, config.join_key,
        null_order_override=null_count,
        keyed_order_total_override=keyed_total,
    )
    semantics["order_rows_visible_to_join"] = len(orders)
    projected = _project_detail(detail, config)
    return _result_package(plan, snapshots, projected, semantics, batches,
                           total_batches, config)


# --------------------------------------------------------------------------
# 执行路径二：整表读取对照
# --------------------------------------------------------------------------

def execute_full_scan(config: QueryConfig, snapshots: dict[str, SourceSnapshot]) -> dict:
    grouped = split_by_source(config.filters)
    plan = _plan_full_scan(grouped)
    c_snap = snapshots[CUSTOMER_SRC]
    o_snap = snapshots[ORDER_SRC]

    c_cols = _columns_for_source(CUSTOMER_SRC, config)
    o_cols = _columns_for_source(ORDER_SRC, config)
    c_sql = _select_sql(CUSTOMERS_TABLE.name, c_cols)
    o_sql = _select_sql(ORDERS_TABLE.name, o_cols)
    plan["explain_query_plan"] = {
        CUSTOMER_SRC: c_snap.explain(c_sql, []),
        ORDER_SRC: o_snap.explain(o_sql, []),
    }

    all_customers = c_snap.query(c_sql, [], mode="full_scan", batch_no=None)
    all_orders = o_snap.query(o_sql, [], mode="full_scan", batch_no=None)

    c_pred = build_py_predicate(grouped.get(CUSTOMER_SRC, []))
    o_pred = build_py_predicate(grouped.get(ORDER_SRC, []))
    customers = [r for r in all_customers if c_pred(r)]
    orders = [r for r in all_orders if o_pred(r)]

    detail, semantics = hash_join(customers, orders, config.join_key)
    semantics["full_scan_rows_read"] = {
        CUSTOMER_SRC: len(all_customers),
        ORDER_SRC: len(all_orders),
    }
    projected = _project_detail(detail, config)
    return _result_package(plan, snapshots, projected, semantics, [], 0, config)


def _result_package(plan, snapshots, projected, semantics, batches,
                    total_batches, config: QueryConfig) -> dict:
    summary = aggregate(projected)
    preview = projected[:DETAIL_PREVIEW_LIMIT]
    return {
        "plan": plan,
        # 深拷贝：对照模式会复用同一快照连接并重置统计，不能保留活动引用
        "source_stats": [copy.deepcopy(snapshots[s].stats.__dict__)
                         for s in (CUSTOMER_SRC, ORDER_SRC)],
        "batches": batches,
        "total_batches": total_batches,
        "semantics": semantics,
        "summary": summary,
        "detail_preview": preview,
        "detail_truncated": len(projected) > DETAIL_PREVIEW_LIMIT,
        "detail_total_rows": len(projected),
        # 完整明细保留在内存中，仅 completed 状态可下载
        "_full_detail": projected,
    }


# --------------------------------------------------------------------------
# 对外入口：单次执行 / 双方式对照
# --------------------------------------------------------------------------

def execute_run(req: RunRequest, cancel_event: threading.Event | None = None) -> dict:
    validate_config(req.config)
    started = time.time()
    snapshots = open_snapshots(
        [CUSTOMER_SRC, ORDER_SRC],
        injected_delays_ms=req.injected_delays_ms,
        timeouts_ms=req.timeouts_ms,
        cancel_event=cancel_event,
    )
    try:
        if req.mode == "pushdown":
            package = execute_pushdown(req.config, snapshots)
        elif req.mode == "full_scan":
            package = execute_full_scan(req.config, snapshots)
        elif req.mode == "compare":
            package = _execute_compare(req, snapshots)
        else:
            raise EngineError(f"未知执行方式: {req.mode}")
        package["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(started))
        package["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        package["elapsed_ms"] = round((time.time() - started) * 1000, 1)
        return package
    finally:
        close_snapshots(snapshots)


def _execute_compare(req: RunRequest, snapshots: dict[str, SourceSnapshot]) -> dict:
    """同一份源快照上先后跑两种方式，核对结果并比较传输量。

    两种方式共用同一组快照连接（即同一份快照数据），
    但统计各自独立记录；任何一种方式失败则整次对照失败。
    """
    # 快照信息在两种方式间共享展示；统计需要分开，先记下快照元数据
    snapshot_meta = {
        s: {"snapshot_at": snapshots[s].stats.snapshot_at,
            "db_path": snapshots[s].stats.db_path,
            "label": snapshots[s].stats.label}
        for s in snapshots
    }

    # --- 方式一：下推 ---
    push = execute_pushdown(req.config, snapshots)
    push_stats = push["source_stats"]
    push_detail = push.pop("_full_detail")

    # 重置各源统计（快照连接不变 → 读到的数据完全一致）
    for snap in snapshots.values():
        snap.stats.queries = []
        snap.stats.rows_returned = 0
        snap.stats.bytes_returned = 0
        snap.stats.filters_pushed_down = []

    # --- 方式二：整表 ---
    full = execute_full_scan(req.config, snapshots)
    full_stats = full["source_stats"]
    full_detail = full.pop("_full_detail")

    same_rows = push_detail == full_detail
    same_summary = push["summary"] == full["summary"]

    def total_bytes(stats):
        return sum(s["bytes_returned"] for s in stats)

    def total_rows(stats):
        return sum(s["rows_returned"] for s in stats)

    comparison = {
        "same_snapshot": True,
        "snapshot_meta": snapshot_meta,
        "row_count_equal": len(push_detail) == len(full_detail),
        "rows_identical": same_rows,
        "summary_identical": same_summary,
        "bytes": {
            "pushdown": total_bytes(push_stats),
            "full_scan": total_bytes(full_stats),
            "saved_by_pushdown": total_bytes(full_stats) - total_bytes(push_stats),
            "pushdown_ratio_pct": round(
                100.0 * total_bytes(push_stats) / total_bytes(full_stats), 2
            ) if total_bytes(full_stats) else 0.0,
        },
        "rows_transferred": {
            "pushdown": total_rows(push_stats),
            "full_scan": total_rows(full_stats),
        },
    }
    return {
        "mode": "compare",
        "plan": {"pushdown": push["plan"], "full_scan": full["plan"]},
        "source_stats": {
            "pushdown": push_stats,
            "full_scan": full_stats,
            "snapshot_meta": snapshot_meta,
        },
        "batches": push["batches"],
        "total_batches": push["total_batches"],
        "semantics": push["semantics"],
        "summary": push["summary"],
        "detail_preview": push["detail_preview"],
        "detail_truncated": push["detail_truncated"],
        "detail_total_rows": push["detail_total_rows"],
        "comparison": comparison,
        "_full_detail": push_detail,
    }
