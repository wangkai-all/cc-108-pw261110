"""CSV 导出：仅对 completed 的运行开放，列名与明细一致。"""
from __future__ import annotations

import csv
import io

# UTF-8 BOM（U+FEFF 的转义写法），让 Excel 正确识别中文
UTF8_BOM = "\ufeff"


def to_csv(detail_rows: list[dict]) -> bytes:
    buf = io.StringIO()
    buf.write(UTF8_BOM)
    if detail_rows:
        columns = list(detail_rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in detail_rows:
            writer.writerow(row)
    # 零行时仍输出带 BOM 的空内容：零行是合法的正式结果，而非缺失结果
    return buf.getvalue().encode("utf-8")
