"""执行编排：后台线程运行、状态轮询、取消与结果生命周期。

状态机：running -> completed | failed | cancelled
- 只有 completed 才持有可下载的正式结果；
- failed/cancelled 不产生下载入口，失败批次绝不被当作空结果；
- 取消后引擎在当前批次边界（或源睡眠期间）停止，不再发出新请求。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .engine import EngineError, execute_run
from .predicates import Filter
from .schema import DEFAULT_BATCH_SIZE, SOURCES
from .sources import BatchFailed, QueryCancelled, QueryTimeout


@dataclass
class RunRecord:
    run_id: str
    status: str = "running"
    created_at: str = ""
    finished_at: str | None = None
    request_echo: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    error_kind: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class RunManager:
    def __init__(self, max_runs: int = 50):
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()
        self._max_runs = max_runs

    # ---- 提交与执行 --------------------------------------------------------

    def submit(self, req) -> RunRecord:
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(
            run_id=run_id,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            request_echo=_echo_request(req),
        )
        with self._lock:
            self._runs[run_id] = record
            self._evict_locked()
        thread = threading.Thread(
            target=self._run, args=(record, req), name=f"run-{run_id}", daemon=True
        )
        thread.start()
        return record

    def _run(self, record: RunRecord, req) -> None:
        try:
            package = execute_run(req, cancel_event=record.cancel_event)
            record.result = package
            record.status = "completed"
        except QueryCancelled as exc:
            record.status = "cancelled"
            record.error = str(exc) or "用户已取消，剩余批次请求未发出"
            record.error_kind = "cancelled"
        except (BatchFailed, QueryTimeout) as exc:
            record.status = "failed"
            record.error = str(exc)
            record.error_kind = type(exc).__name__
        except (EngineError, ValueError, KeyError) as exc:
            record.status = "failed"
            record.error = f"请求或执行错误: {exc}"
            record.error_kind = type(exc).__name__
        except Exception as exc:  # 任何意外失败都显式失败，不产出结果
            record.status = "failed"
            record.error = f"未预期错误: {type(exc).__name__}: {exc}"
            record.error_kind = type(exc).__name__
        finally:
            record.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if record is None or record.status != "running":
            return False
        record.cancel_event.set()
        return True

    # ---- 查询 --------------------------------------------------------------

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def public_status(self, run_id: str) -> dict | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        payload = {
            "run_id": record.run_id,
            "status": record.status,
            "created_at": record.created_at,
            "finished_at": record.finished_at,
            "request": record.request_echo,
            "error": record.error,
            "error_kind": record.error_kind,
            "downloadable": record.status == "completed",
        }
        if record.status == "completed" and record.result is not None:
            res = record.result
            payload["result"] = {k: v for k, v in res.items() if k != "_full_detail"}
        return payload

    def full_detail(self, run_id: str) -> list[dict] | None:
        record = self._runs.get(run_id)
        if record is None or record.status != "completed" or record.result is None:
            return None
        return record.result["_full_detail"]

    def _evict_locked(self) -> None:
        if len(self._runs) <= self._max_runs:
            return
        finished = sorted(
            (r for r in self._runs.values() if r.status != "running"),
            key=lambda r: r.created_at,
        )
        for record in finished[: len(self._runs) - self._max_runs]:
            self._runs.pop(record.run_id, None)


# --------------------------------------------------------------------------
# 请求解析（HTTP 层 JSON -> 引擎对象）
# --------------------------------------------------------------------------

def parse_run_request(body: dict):
    from .engine import QueryConfig, RunRequest

    mode = body.get("mode", "pushdown")
    if mode not in ("pushdown", "full_scan", "compare"):
        raise ValueError(f"未知执行方式: {mode}")

    col_types = {s: SOURCES[s]["table"].col_types for s in SOURCES}

    raw_select = body.get("select_columns") or {}
    select_columns = {}
    for source in SOURCES:
        cols = raw_select.get(source) or list(SOURCES[source]["default_select"])
        select_columns[source] = list(cols)

    filters = [Filter.from_dict(f, col_types) for f in (body.get("filters") or [])]

    batch_size = int(body.get("batch_size") or DEFAULT_BATCH_SIZE)

    fail_batch_no = {}
    for source, no in (body.get("fail_batch_no") or {}).items():
        if source not in SOURCES or no in (None, ""):
            continue
        fail_batch_no[source] = int(no)

    config = QueryConfig(
        select_columns=select_columns,
        filters=filters,
        join_key=body.get("join_key") or "customer_id",
        batch_size=batch_size,
        fail_batch_no=fail_batch_no,
    )

    def _per_source_ints(raw: dict, default: int) -> dict[str, int]:
        out = {}
        for source in SOURCES:
            val = (raw or {}).get(source)
            out[source] = int(val) if val not in (None, "") else default
        return out

    return RunRequest(
        mode=mode,
        config=config,
        injected_delays_ms=_per_source_ints(body.get("injected_delays_ms"), 0),
        timeouts_ms=_per_source_ints(body.get("timeouts_ms"), 5000),
    )


def _echo_request(req) -> dict:
    return {
        "mode": req.mode,
        "join_key": req.config.join_key,
        "batch_size": req.config.batch_size,
        "select_columns": req.config.select_columns,
        "filters": [f.__dict__ for f in req.config.filters],
        "injected_delays_ms": req.injected_delays_ms,
        "timeouts_ms": req.timeouts_ms,
        "fail_batch_no": req.config.fail_batch_no,
    }
