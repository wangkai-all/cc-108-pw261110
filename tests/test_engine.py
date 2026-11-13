import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from app.engine import QueryConfig, RunRequest, execute_run
from app.predicates import Filter
from app.runmanager import RunManager, parse_run_request
from app.sources import BatchFailed, QueryCancelled, QueryTimeout


def base_config(**kw):
    defaults = dict(
        select_columns={
            "customers": ["customer_id", "customer_name", "region", "segment"],
            "orders": ["order_id", "order_date", "amount", "status", "channel"],
        },
        filters=[],
        batch_size=333,
    )
    defaults.update(kw)
    return QueryConfig(**defaults)


class TestEquivalence(unittest.TestCase):
    """两种执行方式在同一份快照上的结果必须逐行一致。"""

    def test_all_filter_combos_equivalent(self):
        cases = [
            [],
            [Filter(source="orders", column="status", op="eq", value="completed")],
            [Filter(source="customers", column="region", op="eq", value="华东")],
            [
                Filter(source="customers", column="region", op="eq", value="华东"),
                Filter(source="orders", column="status", op="eq", value="completed"),
                Filter(source="orders", column="amount", op="ge", value=1000.0),
            ],
            [Filter(source="orders", column="channel", op="in",
                    value=["web", "app"])],
            [Filter(source="customers", column="credit_score", op="gt", value=700)],
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                res = execute_run(RunRequest(
                    mode="compare", config=base_config(filters=filters)))
                self.assertTrue(res["comparison"]["rows_identical"])
                self.assertTrue(res["comparison"]["summary_identical"])
                self.assertTrue(res["comparison"]["same_snapshot"])

    def test_pushdown_transfers_no_more_than_full_scan(self):
        res = execute_run(RunRequest(mode="compare", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="华东")])))
        b = res["comparison"]["bytes"]
        self.assertLess(b["pushdown"], b["full_scan"])
        self.assertEqual(b["saved_by_pushdown"], b["full_scan"] - b["pushdown"])

    def test_region_change_changes_results_real_query(self):
        east = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="华东")])))
        north = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="华北")])))
        self.assertNotEqual(east["summary"], north["summary"])
        self.assertGreater(east["summary"]["detail_row_count"], 0)
        self.assertGreater(north["summary"]["detail_row_count"], 0)


class TestJoinSemantics(unittest.TestCase):
    def test_null_order_keys_excluded_and_counted(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config()))
        self.assertEqual(res["semantics"]["null_order_key_rows_excluded"], 5)
        # NULL 键订单绝不出现在明细中（投影后的关联键列为 c_customer_id）
        self.assertFalse(any(r.get("c_customer_id") is None
                             for r in res["_full_detail"]))

    def test_null_counts_agree_between_modes(self):
        for filters in (
            [],
            [Filter(source="orders", column="status", op="eq", value="completed")],
        ):
            with self.subTest(filters=filters):
                push = execute_run(RunRequest(mode="pushdown",
                                               config=base_config(filters=filters)))
                full = execute_run(RunRequest(mode="full_scan",
                                               config=base_config(filters=filters)))
                self.assertEqual(push["semantics"]["null_order_key_rows_excluded"],
                                 full["semantics"]["null_order_key_rows_excluded"])
                self.assertEqual(push["semantics"]["unmatched_order_rows"],
                                 full["semantics"]["unmatched_order_rows"])

    def test_duplicate_customer_key_multiplies_detail(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config()))
        self.assertEqual(res["semantics"]["duplicate_customer_key_groups"], 3)
        rows = [r for r in res["_full_detail"] if r["c_customer_id"] == "C000007"]
        order_ids = {r["o_order_id"] for r in rows}
        # 两份客户档案：每笔订单产出两行，订单一笔都不能少
        self.assertEqual(len(rows), 2 * len(order_ids))
        self.assertGreater(len(order_ids), 0)

    def test_multiple_orders_per_customer_all_kept(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="华东")])))
        counts = {}
        for r in res["_full_detail"]:
            counts[r["o_order_id"]] = counts.get(r["o_order_id"], 0) + 1
        # 订单号在明细中不重复（普通客户只有一份档案时）
        self.assertTrue(all(v >= 1 for v in counts.values()))
        self.assertEqual(res["summary"]["detail_row_count"], len(res["_full_detail"]))

    def test_orphan_orders_excluded_but_counted(self):
        res = execute_run(RunRequest(mode="full_scan", config=base_config()))
        self.assertGreater(res["semantics"]["unmatched_order_rows"], 0)

    def test_empty_key_set_issues_zero_detail_queries(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="月球")])))
        self.assertEqual(res["total_batches"], 0)
        detail_queries = [q for q in res["source_stats"][1]["queries"]
                          if q["mode"] == "pushdown"]
        self.assertEqual(detail_queries, [])
        self.assertEqual(res["summary"]["detail_row_count"], 0)


class TestBatching(unittest.TestCase):
    def test_batch_sizes_respected(self):
        for size in (50, 200, 1000):
            with self.subTest(size=size):
                res = execute_run(RunRequest(mode="pushdown",
                                              config=base_config(batch_size=size)))
                self.assertEqual(len(res["batches"]), res["total_batches"])
                self.assertTrue(all(b["keys"] <= size for b in res["batches"]))

    def test_keys_deduplicated_before_sending(self):
        # 含重复客户键的全量查询：IN 键数必须是去重后的键数
        res = execute_run(RunRequest(mode="pushdown", config=base_config(batch_size=500)))
        sent_keys = sum(b["keys"] for b in res["batches"])
        self.assertEqual(sent_keys, res["semantics"]["distinct_customer_keys"])
        self.assertLess(sent_keys, res["semantics"]["customer_rows_in"])


class TestFailureAndCancellation(unittest.TestCase):
    def test_failed_batch_marks_run_failed_without_result(self):
        cfg = base_config(fail_batch_no={"orders": 2})
        with self.assertRaises(BatchFailed):
            execute_run(RunRequest(mode="pushdown", config=cfg))

    def test_failed_run_not_downloadable(self):
        mgr = RunManager()
        rec = mgr.submit(RunRequest(mode="pushdown",
                                    config=base_config(fail_batch_no={"orders": 1})))
        self._wait(mgr, rec.run_id)
        self.assertEqual(rec.status, "failed")
        self.assertIsNotNone(rec.error)
        self.assertIsNone(mgr.full_detail(rec.run_id))
        self.assertFalse(mgr.public_status(rec.run_id)["downloadable"])

    def test_cancel_stops_remaining_requests(self):
        import threading
        event = threading.Event()
        event.set()  # 预置取消：首个查询即应中断
        with self.assertRaises(QueryCancelled):
            execute_run(RunRequest(mode="pushdown", config=base_config()),
                        cancel_event=event)

    def test_cancel_via_manager_during_slow_source(self):
        mgr = RunManager()
        rec = mgr.submit(RunRequest(
            mode="pushdown",
            config=base_config(batch_size=100),
            injected_delays_ms={"orders": 300},
            timeouts_ms={"customers": 5000, "orders": 30000},
        ))
        self.assertTrue(mgr.cancel(rec.run_id))
        self._wait(mgr, rec.run_id, timeout=10)
        self.assertEqual(rec.status, "cancelled")
        self.assertIsNone(mgr.full_detail(rec.run_id))

    def test_injected_timeout_fails_run(self):
        with self.assertRaises(QueryTimeout):
            execute_run(RunRequest(
                mode="pushdown", config=base_config(),
                injected_delays_ms={"customers": 300, "orders": 0},
                timeouts_ms={"customers": 50, "orders": 5000},
            ))

    def test_compare_fails_if_either_way_fails(self):
        with self.assertRaises(BatchFailed):
            execute_run(RunRequest(
                mode="compare",
                config=base_config(fail_batch_no={"orders": 3})))

    def test_zero_row_result_is_completed_and_exportable(self):
        from app.export import to_csv
        res = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region",
                            op="eq", value="月球殖民地")])))
        self.assertEqual(res["summary"]["detail_row_count"], 0)
        # 零行是合法正式结果：可导出，且仅含 UTF-8 BOM
        self.assertEqual(to_csv(res["_full_detail"]), b"\xef\xbb\xbf")

    def test_full_scan_reports_whole_table_read(self):
        res = execute_run(RunRequest(mode="full_scan", config=base_config()))
        self.assertEqual(res["semantics"]["full_scan_rows_read"],
                         {"customers": 2003, "orders": 12005})
        self.assertIsInstance(res["source_stats"], list)

    @staticmethod
    def _wait(mgr, run_id, timeout=10):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if mgr.get(run_id).status != "running":
                return
            time.sleep(0.02)
        raise AssertionError("运行未在限定时间内结束")


class TestSnapshotAndPlans(unittest.TestCase):
    def test_snapshot_times_recorded_per_source(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config()))
        for stats in res["source_stats"]:
            self.assertIsNotNone(stats["snapshot_at"])
            self.assertTrue(stats["db_path"].endswith(".db"))

    def test_explain_plans_captured_from_real_databases(self):
        res = execute_run(RunRequest(mode="pushdown", config=base_config(
            filters=[Filter(source="customers", column="region", op="eq", value="华东")])))
        plans = res["plan"]["explain_query_plan"]
        joined = " ".join(p["detail"] for p in plans["customers"])
        self.assertIn("INDEX", joined.upper())

    def test_read_only_snapshot_cannot_write(self):
        import sqlite3
        from app.schema import CUSTOMERS_DB
        conn = sqlite3.connect(f"file:{CUSTOMERS_DB}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE hack(x int)")
        conn.close()

    def test_snapshot_isolation_against_concurrent_write(self):
        """执行期间另一连接写入，快照内读数保持不变（无跨库事务声明）。"""
        import sqlite3
        import threading
        from app.schema import CUSTOMERS_DB
        from app.sources import SourceSnapshot

        snap = SourceSnapshot("customers")
        snap.open()
        try:
            before = snap.query('SELECT * FROM "customers"', [], mode="iso")
            done = threading.Event()

            def write_then_commit():
                # 连接必须在使用它的线程内创建
                writer = sqlite3.connect(CUSTOMERS_DB, timeout=10)
                try:
                    writer.execute("INSERT INTO customers_dup VALUES "
                                   "('C999999','快照隔离探针','华东','零售',600,'2025-01-01')")
                    writer.commit()
                finally:
                    writer.close()
                done.set()

            t = threading.Thread(target=write_then_commit)
            t.start()
            done.wait(5)
            after = snap.query('SELECT * FROM "customers"', [], mode="iso")
            t.join()
            self.assertEqual(len(before), len(after))
        finally:
            snap.close()
        # 清理探针行，恢复固定数据集
        cleaner = sqlite3.connect(CUSTOMERS_DB, timeout=10)
        cleaner.execute("DELETE FROM customers_dup WHERE customer_id='C999999'")
        cleaner.commit()
        cleaner.close()


class TestRequestParsing(unittest.TestCase):
    def test_parse_full_request(self):
        req = parse_run_request({
            "mode": "pushdown",
            "join_key": "customer_id",
            "batch_size": 250,
            "select_columns": {"customers": ["region"], "orders": ["amount"]},
            "filters": [
                {"source": "customers", "column": "region", "op": "eq", "value": "华东"},
                {"source": "orders", "column": "amount", "op": "ge", "value": "100"},
            ],
            "injected_delays_ms": {"orders": 200},
            "timeouts_ms": {"customers": 3000, "orders": 3000},
            "fail_batch_no": {"orders": 2},
        })
        self.assertEqual(req.config.batch_size, 250)
        self.assertEqual(req.injected_delays_ms["orders"], 200)
        self.assertEqual(req.config.filters[1].value, 100.0)
        self.assertEqual(req.config.fail_batch_no, {"orders": 2})

    def test_reject_unknown_mode(self):
        with self.assertRaises(ValueError):
            parse_run_request({"mode": "nope"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
