"""两个独立数据源的结构定义与默认配置。

两个源分别是磁盘上两个完全独立的 SQLite 数据库文件，
应用层不做任何分布式/全局事务的假设。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

# 两个独立数据库（各自独立连接、独立快照、独立失败域）
CUSTOMERS_DB = os.path.join(DATA_DIR, "customers_db.db")
ORDERS_DB = os.path.join(DATA_DIR, "orders_db.db")

JOIN_KEY = "customer_id"  # 两表通过 customer_id 关联


@dataclass(frozen=True)
class Column:
    name: str
    decl_type: str  # SQLite 声明类型，用于值转换


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]

    def col(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def col_types(self) -> dict[str, str]:
        return {c.name: c.decl_type for c in self.columns}


CUSTOMERS_TABLE = Table(
    "customers",
    (
        Column("customer_id", "TEXT"),
        Column("customer_name", "TEXT"),
        Column("region", "TEXT"),
        Column("segment", "TEXT"),
        Column("credit_score", "INTEGER"),
        Column("signup_date", "TEXT"),
    ),
)

ORDERS_TABLE = Table(
    "orders",
    (
        Column("order_id", "TEXT"),
        Column("customer_id", "TEXT"),
        Column("order_date", "TEXT"),
        Column("amount", "REAL"),
        Column("status", "TEXT"),
        Column("channel", "TEXT"),
    ),
)

# 源注册表：order 决定界面与结果中的展示顺序
SOURCES: dict[str, dict] = {
    "customers": {
        "label": "客户库",
        "db_path": CUSTOMERS_DB,
        "table": CUSTOMERS_TABLE,
        "default_select": ("customer_id", "customer_name", "region", "segment"),
    },
    "orders": {
        "label": "订单库",
        "db_path": ORDERS_DB,
        "table": ORDERS_TABLE,
        "default_select": ("order_id", "order_date", "amount", "status", "channel"),
    },
}

# 另一个源通过该外键列引用本源主键
FOREIGN_KEY = {
    "customers": ("orders", JOIN_KEY),
}

DEFAULT_BATCH_SIZE = 200
DEFAULT_TIMEOUT_MS = 5000
# 界面上明细最多直接渲染的行数，完整结果走 CSV 下载
DETAIL_PREVIEW_LIMIT = 500

REGIONS = ["华东", "华北", "华南", "华中", "西南", "西北", "东北"]
ORDER_STATUSES = ["completed", "pending", "cancelled"]
CHANNELS = ["web", "app", "store", "partner"]
SEGMENTS = ["零售", "企业", "政府", "个人"]
