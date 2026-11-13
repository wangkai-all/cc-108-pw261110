"""HTTP 服务：分析页静态资源 + 执行/轮询/取消/下载 API。

仅依赖 Python 标准库。所有数字均来自真实查询，服务端不内置任何演示数值。
"""
from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 允许以 `python3 -I app/server.py` 直接运行（隔离模式下 cwd 不在 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.runmanager import RunManager, parse_run_request
from app.export import to_csv
from app.schema import (
    CHANNELS, DEFAULT_BATCH_SIZE, ORDER_STATUSES, REGIONS, SEGMENTS, SOURCES,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

MANAGER = RunManager()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CrossDBDemo/1.0"

    def log_message(self, fmt, *args):  # 安静一点，失败仍会走异常处理
        pass

    # ---- 工具 ---------------------------------------------------------------

    def _send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("请求体为空")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_static(self, rel_path: str):
        # 防目录穿越
        safe = os.path.normpath(os.path.join(WEB_DIR, rel_path))
        if not safe.startswith(WEB_DIR + os.sep) and safe != WEB_DIR:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if os.path.isdir(safe):
            safe = os.path.join(safe, "index.html")
        if not os.path.isfile(safe):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ext = os.path.splitext(safe)[1]
        with open(safe, "rb") as fh:
            body = fh.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 路由 ---------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                return self._handle_config()
            if path.startswith("/api/runs/") and path.endswith("/status"):
                run_id = path.split("/")[3]
                return self._handle_status(run_id)
            if path.startswith("/api/runs/") and path.endswith("/download"):
                run_id = path.split("/")[3]
                return self._handle_download(run_id)
            if path == "/api/health":
                return self._send_json({"ok": True})
            return self._send_static("index.html" if path == "/" else path.lstrip("/"))
        except BrokenPipeError:
            pass
        except Exception as exc:  # 兜底：错误显式返回，不伪装成空数据
            self._send_json({"error": f"{type(exc).__name__}: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/runs":
                return self._handle_submit()
            if path.startswith("/api/runs/") and path.endswith("/cancel"):
                run_id = path.split("/")[3]
                return self._handle_cancel(run_id)
            self.send_error(HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except (ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)

    # ---- API 处理 -----------------------------------------------------------

    def _handle_config(self):
        self._send_json({
            "sources": {
                name: {
                    "label": meta["label"],
                    "table": meta["table"].name,
                    "db_path": meta["db_path"],
                    "columns": [c.__dict__ for c in meta["table"].columns],
                    "default_select": list(meta["default_select"]),
                }
                for name, meta in SOURCES.items()
            },
            "join_key": "customer_id",
            "default_batch_size": DEFAULT_BATCH_SIZE,
            "enums": {
                "region": REGIONS,
                "order_status": ORDER_STATUSES,
                "channel": CHANNELS,
                "segment": SEGMENTS,
            },
            "ops": [
                {"op": "eq", "label": "= 等于"},
                {"op": "ne", "label": "!= 不等于"},
                {"op": "gt", "label": "> 大于"},
                {"op": "ge", "label": ">= 大于等于"},
                {"op": "lt", "label": "< 小于"},
                {"op": "le", "label": "<= 小于等于"},
                {"op": "in", "label": "IN（逗号分隔）"},
                {"op": "like", "label": "LIKE 模糊"},
                {"op": "isnull", "label": "IS NULL"},
                {"op": "notnull", "label": "IS NOT NULL"},
            ],
        })

    def _handle_submit(self):
        body = self._read_json_body()
        req = parse_run_request(body)
        record = MANAGER.submit(req)
        self._send_json({"run_id": record.run_id, "status": record.status},
                        HTTPStatus.ACCEPTED)

    def _handle_status(self, run_id: str):
        payload = MANAGER.public_status(run_id)
        if payload is None:
            self._send_json({"error": "运行不存在或已被清理"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(payload)

    def _handle_cancel(self, run_id: str):
        ok = MANAGER.cancel(run_id)
        if not ok:
            self._send_json({"error": "运行不存在或已结束，无法取消"},
                            HTTPStatus.CONFLICT)
            return
        self._send_json({"run_id": run_id, "cancelling": True})

    def _handle_download(self, run_id: str):
        detail = MANAGER.full_detail(run_id)
        if detail is None:
            # 未完成/失败/取消：明确拒绝下载，防止把不完整结果当正式结果
            self._send_json({"error": "只有整次完成的运行才能下载正式结果"},
                            HTTPStatus.CONFLICT)
            return
        body = to_csv(detail)
        filename = f"result_{run_id}.csv"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="跨库订单分析演示服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    httpd = make_server(args.host, args.port)
    print(f"服务已启动: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
