"""源端访问层：每个数据库独立的只读连接与快照。

关键设计：
- 每个源在执行开始时各自打开只读连接，快照时间独立记录；
  两个数据库之间没有、也不声称存在跨库全局事务。
- WAL 模式下读连接开启一个只读事务即可获得稳定快照，
  执行期间其它进程的写入不会改变本次读到的数据。
- 支持注入延迟/超时与指定批次失败，全部走真实查询路径。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from .schema import SOURCES, Column, Table


class QueryCancelled(Exception):
    """用户取消，区别于查询失败。"""


class QueryTimeout(Exception):
    """源端执行超过截止时间（含注入延迟）。"""


class BatchFailed(Exception):
    """指定批次被注入失败。"""


def payload_bytes(rows: list[dict]) -> int:
    """应用层实际从该源接收到的数据量（UTF-8 JSON 编码字节）。

    两种执行方式用同一口径计量，保证对照比较公平。
    """
    return len(json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8"))


def _interruptible_sleep(seconds: float, cancel_event, check_interval: float = 0.05) -> None:
    """可被取消打断的睡眠；取消时抛出 QueryCancelled。"""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelled("已取消")
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        time.sleep(min(check_interval, remain))


@dataclass
class SourceStats:
    """单个源在一次执行中的真实统计。"""

    source: str
    label: str
    db_path: str
    snapshot_at: str | None = None
    snapshot_epoch: float | None = None
    queries: list[dict] = field(default_factory=list)
    rows_returned: int = 0          # 该源实际返回的行数合计
    bytes_returned: int = 0         # 该源实际传回应用层的字节合计
    filters_pushed_down: list[str] = field(default_factory=list)
    error: str | None = None

    def record_query(self, sql: str, params: list, rows: list[dict], elapsed_ms: float,
                     mode: str, batch_no: int | None = None) -> None:
        self.queries.append({
            "sql": sql,
            "params": params,
            "row_count": len(rows),
            "bytes": payload_bytes(rows),
            "elapsed_ms": round(elapsed_ms, 3),
            "mode": mode,
            "batch_no": batch_no,
        })
        self.rows_returned += len(rows)
        self.bytes_returned += payload_bytes(rows)


class SourceSnapshot:
    """一个源在一次执行期间的只读快照连接。"""

    def __init__(self, source: str, *, injected_delay_ms: int = 0,
                 timeout_ms: int = 5000, cancel_event=None):
        if source not in SOURCES:
            raise KeyError(f"未知数据源: {source}")
        meta = SOURCES[source]
        self.source = source
        self.label = meta["label"]
        self.db_path = meta["db_path"]
        self.table: Table = meta["table"]
        self.injected_delay_ms = injected_delay_ms
        self.timeout_ms = timeout_ms
        self.cancel_event = cancel_event
        self.stats = SourceStats(source=source, label=self.label, db_path=self.db_path)
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    # ---- 生命周期 ----------------------------------------------------------

    def open(self) -> None:
        # uri 只读模式，确保执行层无法修改源数据
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA query_only = ON")
        # 开启一个只读事务以固定快照（WAL 下不阻塞写者）
        conn.execute("BEGIN")
        conn.execute("SELECT 1")  # 触发事务真正建立
        self._conn = conn
        now = time.time()
        self.stats.snapshot_epoch = now
        self.stats.snapshot_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    def close(self) -> None:
        if self._closed or self._conn is None:
            return
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        self._conn.close()
        self._closed = True

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 查询 --------------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise QueryCancelled("已取消")

    def explain(self, sql: str, params: list) -> list[dict]:
        """取得该 SQL 在本源上的真实 EXPLAIN QUERY PLAN。"""
        cur = self._conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query(self, sql: str, params: list, *, mode: str,
              batch_no: int | None = None,
              fail_this_batch: bool = False) -> list[dict]:
        """执行一次真实查询并记录统计；支持取消、截止时间与注入故障。"""
        self._check_cancelled()

        if fail_this_batch:
            # 故障批次：不返回任何数据，显式失败，调用方不得当空结果处理
            self.stats.queries.append({
                "sql": sql, "params": params, "row_count": 0, "bytes": 0,
                "elapsed_ms": 0.0, "mode": mode, "batch_no": batch_no,
                "injected_failure": True,
            })
            raise BatchFailed(
                f"{self.label} 第 {batch_no} 批查询被注入失败（已停止，未作为空结果处理）"
            )

        started = time.monotonic()
        deadline = started + self.timeout_ms / 1000.0

        def progress_handler():
            # SQLite 在长查询期间周期性回调，用于响应取消与截止时间
            if self.cancel_event is not None and self.cancel_event.is_set():
                return 1  # 中断当前语句
            if time.monotonic() > deadline:
                return 1
            return 0

        conn = self._conn
        conn.set_progress_handler(progress_handler, 1000)
        try:
            cur = conn.execute(sql, params)
            raw_rows = cur.fetchall()
            # 模拟源端延迟（如网络/慢查询），可被取消打断并受同一截止时间约束
            if self.injected_delay_ms > 0:
                _interruptible_sleep(self.injected_delay_ms / 1000.0, self.cancel_event)
                if time.monotonic() > deadline:
                    raise QueryTimeout(
                        f"{self.label} 查询超时（限额 {self.timeout_ms}ms，含注入延迟 "
                        f"{self.injected_delay_ms}ms）"
                    )
        except sqlite3.OperationalError as exc:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise QueryCancelled("已取消") from exc
            if "interrupted" in str(exc).lower():
                if time.monotonic() > deadline:
                    raise QueryTimeout(
                        f"{self.label} 查询超时（限额 {self.timeout_ms}ms，含注入延迟 "
                        f"{self.injected_delay_ms}ms）"
                    ) from exc
                raise QueryCancelled("已取消") from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)

        elapsed_ms = (time.monotonic() - started) * 1000
        rows = [dict(r) for r in raw_rows]
        self.stats.record_query(sql, list(params), rows, elapsed_ms, mode, batch_no)
        self._check_cancelled()
        return rows


def open_snapshots(sources: list[str], *, injected_delays_ms: dict[str, int] | None = None,
                   timeouts_ms: dict[str, int] | None = None, cancel_event=None
                   ) -> dict[str, SourceSnapshot]:
    """为一次执行打开各源独立快照。任一源打开失败则整体失败（不产生结果）。"""
    injected_delays_ms = injected_delays_ms or {}
    timeouts_ms = timeouts_ms or {}
    snapshots: dict[str, SourceSnapshot] = {}
    try:
        for src in sources:
            snap = SourceSnapshot(
                src,
                injected_delay_ms=injected_delays_ms.get(src, 0),
                timeout_ms=timeouts_ms.get(src, 5000),
                cancel_event=cancel_event,
            )
            snap.open()
            snapshots[src] = snap
    except Exception:
        for snap in snapshots.values():
            snap.close()
        raise
    return snapshots


def close_snapshots(snapshots: dict[str, SourceSnapshot]) -> None:
    for snap in snapshots.values():
        snap.close()
