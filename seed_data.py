"""生成两个独立的 SQLite 源数据库及演示数据。

- 两个库是完全独立的磁盘文件，各自建表写入，不存在任何跨库 DDL/事务；
- 固定随机种子，保证样本可复现、对照实验可重复；
- 显式构造边界数据：重复客户键、NULL 关联键、无订单客户、孤儿订单、
  同一客户多笔订单，用于验证连接语义。
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.schema import (
    CHANNELS, CUSTOMERS_DB, DATA_DIR, ORDER_STATUSES, ORDERS_DB, REGIONS,
    SEGMENTS,
)

SEED = 20261113


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if os.path.exists(side):
            os.remove(side)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def build_databases(customer_count: int = 2000, order_count: int = 12000,
                    verbose: bool = True) -> dict:
    rng = random.Random(SEED)
    start = date(2024, 1, 1)

    # ---------------- 客户库 ----------------
    cconn = _connect(CUSTOMERS_DB)
    cconn.execute("""
        CREATE TABLE customers_core (
            customer_id   TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            region        TEXT NOT NULL,
            segment       TEXT NOT NULL,
            credit_score  INTEGER,
            signup_date   TEXT NOT NULL
        )
    """)
    customers = []
    for i in range(1, customer_count + 1):
        cid = f"C{i:06d}"
        customers.append((
            cid,
            f"客户{i:06d}",
            rng.choice(REGIONS),
            rng.choice(SEGMENTS),
            rng.randint(350, 850),
            (start + timedelta(days=rng.randint(0, 700))).isoformat(),
        ))

    # 边界 1：重复客户键——同一个 customer_id 出现两行（不同地区/名称），
    # 用于验证 IN 去重传输但连接时保留乘法语义
    dup_customers = []
    for cid in ("C000007", "C000042", "C000100"):
        base = next(r for r in customers if r[0] == cid)
        dup_customers.append((
            cid,
            base[1] + "-重复档案",
            rng.choice([r for r in REGIONS if r != base[2]]),
            rng.choice(SEGMENTS),
            rng.randint(350, 850),
            base[5],
        ))
    # 主键约束下重复键需要独立表来真实模拟"脏数据"：核心客户表有主键约束，
    # 另建一张无约束客户表存放重复档案，视图把两部分合并，业务侧只读 customers
    cconn.executemany(
        "INSERT INTO customers_core VALUES (?, ?, ?, ?, ?, ?)", customers
    )
    cconn.execute("""
        CREATE TABLE customers_dup (
            customer_id   TEXT,
            customer_name TEXT NOT NULL,
            region        TEXT NOT NULL,
            segment       TEXT NOT NULL,
            credit_score  INTEGER,
            signup_date   TEXT NOT NULL
        )
    """)
    cconn.executemany(
        "INSERT INTO customers_dup VALUES (?, ?, ?, ?, ?, ?)", dup_customers
    )
    cconn.execute("""
        CREATE VIEW customers AS
        SELECT * FROM customers_core
        UNION ALL
        SELECT * FROM customers_dup
    """)
    cconn.execute("CREATE INDEX idx_customers_region ON customers_core(region)")
    cconn.commit()

    # ---------------- 订单库（独立数据库文件）----------------
    oconn = _connect(ORDERS_DB)
    oconn.execute("""
        CREATE TABLE orders (
            order_id    TEXT PRIMARY KEY,
            customer_id TEXT,
            order_date  TEXT NOT NULL,
            amount      REAL NOT NULL,
            status      TEXT NOT NULL,
            channel     TEXT NOT NULL
        )
    """)
    orders = []
    for i in range(1, order_count + 1):
        # 大部分订单挂到真实客户；约 2% 为孤儿订单（键在客户库不存在）
        if rng.random() < 0.02:
            cid = f"X{rng.randint(1, 999999):06d}"
        else:
            cid = f"C{rng.randint(1, customer_count):06d}"
        orders.append((
            f"O{i:07d}",
            cid,
            (start + timedelta(days=rng.randint(0, 729))).isoformat(),
            round(rng.uniform(10.0, 20000.0), 2),
            rng.choices(ORDER_STATUSES, weights=[70, 20, 10])[0],
            rng.choice(CHANNELS),
        ))
    # 边界 2：NULL 客户键订单——必须被显式排除并计数，不能静默丢弃
    for i in range(5):
        orders.append((
            f"ON{i:06d}", None,
            (start + timedelta(days=rng.randint(0, 729))).isoformat(),
            round(rng.uniform(10.0, 20000.0), 2),
            rng.choice(ORDER_STATUSES),
            rng.choice(CHANNELS),
        ))
    rng.shuffle(orders)
    oconn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", orders)
    oconn.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
    oconn.execute("CREATE INDEX idx_orders_date ON orders(order_date)")
    oconn.execute("CREATE INDEX idx_orders_status ON orders(status)")
    oconn.commit()

    c_rows = cconn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    o_rows = oconn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    cconn.close()
    oconn.close()

    info = {
        "customers_db": CUSTOMERS_DB,
        "orders_db": ORDERS_DB,
        "customer_rows": c_rows,
        "order_rows": o_rows,
        "duplicate_customer_keys": 3,
        "null_customer_id_orders": 5,
        "orphan_order_ratio": 0.02,
        "seed": SEED,
    }
    if verbose:
        for k, v in info.items():
            print(f"{k}: {v}")
    return info


def main():
    parser = argparse.ArgumentParser(description="生成演示用两个独立源数据库")
    parser.add_argument("--customers", type=int, default=2000)
    parser.add_argument("--orders", type=int, default=12000)
    args = parser.parse_args()
    build_databases(args.customers, args.orders)


if __name__ == "__main__":
    main()
