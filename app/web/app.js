/* 跨库分析页前端逻辑：无框架，全部数据来自 /api 真实查询结果。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let CONFIG = null;
let pollTimer = null;
let currentRunId = null;

// ---------------------------------------------------------------- 初始化

async function init() {
  bindStaticControls();
  try {
    const resp = await fetch("/api/config");
    CONFIG = await resp.json();
    $("#health").textContent = "服务在线";
    $("#health").classList.add("ok");
    renderConfig();
  } catch (err) {
    $("#health").textContent = "服务不可用";
    $("#health").classList.add("bad");
    setStatus("无法加载配置：" + err.message, "failed");
  }
}

function bindStaticControls() {
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
  });
  $("#add-filter").addEventListener("click", () => addFilterRow());
  $("#run-btn").addEventListener("click", submitRun);
  $("#cancel-btn").addEventListener("click", cancelRun);
}

function renderConfig() {
  const joinSelect = $("#join-key");
  joinSelect.innerHTML = "";
  const keyCol = CONFIG.join_key;
  [keyCol].forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    joinSelect.appendChild(opt);
  });
  $("#batch-size").value = CONFIG.default_batch_size;

  for (const [source, meta] of Object.entries(CONFIG.sources)) {
    const box = $("#cols-" + source);
    box.innerHTML = "";
    meta.columns.forEach((col) => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = col.name;
      cb.dataset.source = source;
      cb.checked = meta.default_select.includes(col.name);
      label.append(cb, document.createTextNode(col.name));
      box.appendChild(label);
    });
  }
  addFilterRow("customers", "region", "eq", "华东");
  addFilterRow("orders", "status", "eq", "completed");
}

// ---------------------------------------------------------------- 筛选行

function addFilterRow(source, column, op, value) {
  const tpl = $("#filter-template").content.cloneNode(true);
  const row = tpl.querySelector(".filter-row");
  const srcSel = row.querySelector(".f-source");
  const colSel = row.querySelector(".f-column");
  const opSel = row.querySelector(".f-op");
  const valInput = row.querySelector(".f-value");

  Object.entries(CONFIG.sources).forEach(([key, meta]) => {
    const o = document.createElement("option");
    o.value = key;
    o.textContent = meta.label + " · " + meta.table;
    srcSel.appendChild(o);
  });
  CONFIG.ops.forEach(({ op, label }) => {
    const o = document.createElement("option");
    o.value = op;
    o.textContent = label;
    opSel.appendChild(o);
  });

  const fillColumns = () => {
    const meta = CONFIG.sources[srcSel.value];
    colSel.innerHTML = "";
    meta.columns.forEach((c) => {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = `${c.name} (${c.decl_type})`;
      colSel.appendChild(o);
    });
  };
  srcSel.addEventListener("change", fillColumns);
  opSel.addEventListener("change", () => {
    valInput.disabled = opSel.value === "isnull" || opSel.value === "notnull";
    if (valInput.disabled) valInput.value = "";
  });
  row.querySelector(".f-remove").addEventListener("click", () => row.remove());

  $("#filters").appendChild(row);
  fillColumns();
  if (source) srcSel.value = source;
  fillColumns();
  if (column) colSel.value = column;
  if (op) opSel.value = op;
  if (value !== undefined) valInput.value = value;
}

// ---------------------------------------------------------------- 提交

function collectPayload() {
  const select_columns = {};
  for (const source of Object.keys(CONFIG.sources)) {
    select_columns[source] = $$(`#cols-${source} input:checked`).map((i) => i.value);
  }
  const filters = $$("#filters .filter-row").map((row) => {
    const op = row.querySelector(".f-op").value;
    const f = {
      source: row.querySelector(".f-source").value,
      column: row.querySelector(".f-column").value,
      op,
    };
    if (op !== "isnull" && op !== "notnull") {
      const raw = row.querySelector(".f-value").value.trim();
      if (!raw) throw new Error("存在未填值的筛选条件，请补全或删除该行");
      if (op === "in" && raw.split(",").map((v) => v.trim()).filter(Boolean).length === 0) {
        throw new Error("IN 筛选至少需要一个有效值");
      }
      f.value = raw;
    }
    return f;
  }).filter((f) => f.op === "isnull" || f.op === "notnull" || f.value !== "");

  const num = (id) => {
    const v = $(id).value;
    return v === "" ? null : Number(v);
  };
  return {
    mode: $('input[name="mode"]:checked').value,
    join_key: $("#join-key").value,
    batch_size: Number($("#batch-size").value),
    select_columns,
    filters,
    injected_delays_ms: {
      customers: num("#delay-customers") || 0,
      orders: num("#delay-orders") || 0,
    },
    timeouts_ms: {
      customers: num("#timeout-customers") || 5000,
      orders: num("#timeout-orders") || 5000,
    },
    fail_batch_no: {
      customers: num("#fail-customers"),
      orders: num("#fail-orders"),
    },
  };
}

async function submitRun() {
  let payload;
  try {
    payload = collectPayload();
  } catch (err) {
    setStatus(err.message, "failed");
    return;
  }
  setRunningUi(true);
  setStatus("已提交，等待执行…", "running");
  $("#results").innerHTML = "";
  try {
    const resp = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "提交失败");
    currentRunId = data.run_id;
    pollTimer = setInterval(pollStatus, 250);
  } catch (err) {
    setRunningUi(false);
    setStatus(err.message, "failed");
  }
}

async function pollStatus() {
  if (!currentRunId) return;
  const resp = await fetch(`/api/runs/${currentRunId}/status`);
  const data = await resp.json();
  if (data.status === "running") {
    setStatus("执行中…（可随时取消，取消后不再发出新批次请求）", "running");
    return;
  }
  clearInterval(pollTimer);
  pollTimer = null;
  setRunningUi(false);
  renderFinished(data);
}

async function cancelRun() {
  if (!currentRunId) return;
  $("#cancel-btn").disabled = true;
  setStatus("正在取消…", "running");
  const resp = await fetch(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) setStatus(data.error || "取消失败", "failed");
}

function setRunningUi(running) {
  $("#run-btn").disabled = running;
  $("#cancel-btn").disabled = !running;
}

function setStatus(text, kind) {
  const el = $("#run-status");
  el.textContent = text;
  el.className = "run-status " + (kind || "");
}

// ---------------------------------------------------------------- 渲染

function renderFinished(data) {
  setStatus(
    data.status === "completed" ? "执行完成" :
    data.status === "cancelled" ? "已取消：剩余批次未执行，未生成结果" :
    "执行失败：未生成可下载结果",
    data.status
  );
  const root = $("#results");
  root.innerHTML = "";

  if (data.status !== "completed") {
    root.appendChild(el("div", "banner " + (data.status === "cancelled" ? "warn" : "error"),
      `<b>${data.status === "cancelled" ? "已取消" : "失败"}（${data.error_kind || ""}）</b><br>${escapeHtml(data.error || "")}`));
    root.appendChild(buildRequestCard(data.request));
    return;
  }
  const r = data.result;
  root.appendChild(buildSummaryCards(r, data));
  if (r.comparison) root.appendChild(buildComparison(r));
  root.appendChild(buildSourceStats(r));
  root.appendChild(buildSemantics(r));
  root.appendChild(buildPlan(r));
  root.appendChild(buildDetail(r, data.run_id));
}

function buildSummaryCards(r, data) {
  const card = document.createElement("div");
  card.className = "card";
  const s = r.summary;
  const tiles = [
    ["明细行数", s.detail_row_count, `完整结果 ${r.detail_total_rows} 行`],
    ["有订单的去重客户数", s.distinct_customers_with_orders, "按关联键去重"],
    ["订单金额合计", fmtMoney(s.total_amount), `${s.amount_values_counted} 笔参与求和`],
    ["执行耗时 (ms)", data.result.elapsed_ms, data.result.started_at + " → " + data.result.finished_at],
  ];
  card.innerHTML = `<div class="section-title"><h2>结果汇总</h2>
    <span class="muted">运行 ${escapeHtml(data.run_id)}</span></div>
    <div class="stat-grid">${tiles.map(([l, v, h]) =>
      `<div class="stat-tile"><div class="label">${l}</div>
       <div class="value">${v}</div><div class="hint">${escapeHtml(h)}</div></div>`).join("")}</div>`;
  return card;
}

function buildComparison(r) {
  const c = r.comparison;
  const card = document.createElement("div");
  card.className = "card";
  const ok = c.rows_identical && c.summary_identical;
  const meta = Object.entries(c.snapshot_meta).map(([src, m]) =>
    `<div class="muted">${escapeHtml(m.label)}：快照时间 ${escapeHtml(m.snapshot_at)} · ${escapeHtml(m.db_path)}</div>`
  ).join("");
  card.innerHTML = `
    <div class="section-title"><h2>两种方式对照（同一份源快照）</h2>
      <span class="pill ${ok ? "good" : "crit"}">${ok ? "✓ 逐行一致" : "✗ 结果不一致"}</span></div>
    ${meta}
    <p class="muted" style="margin:.4rem 0">
      行数相等：${c.row_count_equal} ｜ 逐行一致：${c.rows_identical} ｜ 汇总一致：${c.summary_identical}
    </p>
    <div class="legend">
      <span><i style="background:var(--series-1)"></i>键下推+分批</span>
      <span><i style="background:var(--series-2)"></i>整表读取</span>
      <span><i style="background:var(--series-3)"></i>下推节省</span>
    </div>
    ${comparisonBars(c.bytes, "传回应用层的数据量（字节，统一 UTF-8 JSON 口径）")}
    ${comparisonBars(toRowBytes(c.rows_transferred), "源端实际返回行数")}
    <table class="kv-table" style="margin-top:.6rem">
      <tr><td>下推传回字节</td><td class="num">${fmtNum(c.bytes.pushdown)}</td></tr>
      <tr><td>整表传回字节</td><td class="num">${fmtNum(c.bytes.full_scan)}</td></tr>
      <tr><td>下推节省字节</td><td class="num"><b>${fmtNum(c.bytes.saved_by_pushdown)}</b></td></tr>
      <tr><td>下推 / 整表 比例</td><td class="num">${c.bytes.pushdown_ratio_pct}%</td></tr>
      <tr><td>下推返回行数 / 整表返回行数</td>
      <td class="num">${c.rows_transferred.pushdown} / ${c.rows_transferred.full_scan}</td></tr>
    </table>
    <p class="muted">注：两方式共用同一组快照连接，因此读到的是同一份数据；
    系统不存在跨库全局事务，一致性仅指本次快照内的应用层核对。</p>`;
  return card;
}

function toRowBytes(rows) {
  return { pushdown: rows.pushdown, full_scan: rows.full_scan, saved_by_pushdown: rows.full_scan - rows.pushdown };
}

function comparisonBars(b, title) {
  const max = Math.max(b.pushdown, b.full_scan, 1);
  const row = (label, val, cls, denom) => `
    <div class="source-row">
      <header><span>${label}</span><b>${fmtNum(val)}</b></header>
      <div class="bar-track">
        <div class="bar-fill ${cls}" style="width:${Math.max(2, (val / denom) * 100)}%"></div>
        <span class="bar-label">${pct(val, denom)}</span>
      </div>
    </div>`;
  return `<h3>${escapeHtml(title)}</h3>
    <div class="source-grid">
      ${row("键下推 + 分批", b.pushdown, "c1", max)}
      ${row("整表读取", b.full_scan, "c2", max)}
      ${row("下推节省量", b.saved_by_pushdown, "c3", max)}
    </div>`;
}

function buildSourceStats(r) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<div class="section-title"><h2>各源真实统计</h2>
    <span class="muted">行数与字节均为实际查询累计</span></div>`;
  const grid = document.createElement("div");
  grid.className = "source-grid";

  // 统一基准：本次运行中出现过的最大源字节数，保证不同方式间可比
  const allStats = [];
  if (Array.isArray(r.source_stats)) {
    allStats.push({ modeLabel: null, colorClass: "c1", list: r.source_stats });
  } else {
    if (r.source_stats.pushdown) {
      allStats.push({ modeLabel: "键下推 + 分批", colorClass: "c1", list: r.source_stats.pushdown });
    }
    if (r.source_stats.full_scan) {
      allStats.push({ modeLabel: "整表读取", colorClass: "c2", list: r.source_stats.full_scan });
    }
  }
  const maxBytes = Math.max(1, ...allStats.flatMap((g) => g.list.map((s) => s.bytes_returned)));
  allStats.forEach((group) => {
    if (group.modeLabel) {
      const h = document.createElement("h3");
      h.textContent = group.modeLabel;
      h.style.marginTop = "0.6rem";
      grid.appendChild(h);
    }
    group.list.forEach((s) => grid.appendChild(sourceBlock(s, maxBytes, group.colorClass)));
  });
  card.appendChild(grid);
  return card;
}

function sourceBlock(s, maxBytes, colorClass) {
  const div = document.createElement("div");
  div.className = "source-row";
  const width = Math.max(2, (s.bytes_returned / maxBytes) * 100);
  const queries = s.queries || [];
  div.innerHTML = `
    <header><b>${escapeHtml(s.label)}</b><span class="snap-time">快照 ${escapeHtml(s.snapshot_at || "—")}</span></header>
    <div class="metric-line">
      <span>实际返回行数：<b>${fmtNum(s.rows_returned)}</b></span>
      <span>传回字节：<b>${fmtNum(s.bytes_returned)}</b></span>
      <span>查询次数：<b>${queries.length}</b></span>
    </div>
    <div class="metric-line"><span>下推筛选：${
      (s.filters_pushed_down || []).length ? s.filters_pushed_down.map(escapeHtml).join("、") : "无"
    }</span></div>
    <div class="bar-track">
      <div class="bar-fill ${colorClass}" style="width:${width}%"></div>
      <span class="bar-label">${fmtNum(s.bytes_returned)} B</span>
    </div>
    <details><summary>查看 ${queries.length} 条真实 SQL 与耗时</summary>
      ${queries.map((q) => `
        <div class="sql-block">${escapeHtml(q.sql)}
${q.params && q.params.length ? "-- 参数: " + escapeHtml(JSON.stringify(q.params)) : ""}
-- 模式 ${q.mode}${q.batch_no ? " · 第 " + q.batch_no + " 批" : ""} · 返回 ${q.row_count} 行 · ${q.bytes} B · ${q.elapsed_ms} ms
${q.injected_failure ? "★ 该批次被注入失败" : ""}</div>`).join("")}
    </details>`;
  return div;
}

function buildSemantics(r) {
  const m = r.semantics;
  const card = document.createElement("div");
  card.className = "card";
  const rows = [
    ["连接方式", `${m.join_type} ON ${m.join_key}`],
    ["进入连接的客户行数", m.customer_rows_in],
    ["进入连接的订单行数", m.order_rows_in],
    ["去重后客户键数（IN 实际发送）", m.distinct_customer_keys],
    ["重复客户键分组（每组产生乘法明细）", m.duplicate_customer_key_groups],
    ["NULL 客户键行数（不参与连接，显式排除）", m.null_customer_key_rows_excluded],
    ["NULL 订单键行数（不参与连接，显式排除）", m.null_order_key_rows_excluded],
    ["无匹配客户的孤儿订单行数（内连接排除，单独计数）", m.unmatched_order_rows],
    ["匹配上的订单行数", m.matched_order_rows],
    ["匹配的去重键数", m.matched_distinct_keys],
    ["最终明细行数", m.detail_rows],
  ];
  if (m.full_scan_rows_read) {
    rows.push(["整表实际读取行数（客户/订单）",
      `${m.full_scan_rows_read.customers} / ${m.full_scan_rows_read.orders}`]);
  }
  card.innerHTML = `<div class="section-title"><h2>连接与空值/重复键语义</h2>
    <span class="pill c1">明确计数，不静默丢弃</span></div>
    <table class="kv-table"><tbody>${rows.map(([k, v]) =>
      `<tr><td>${escapeHtml(k)}</td><td class="num">${escapeHtml(String(v))}</td></tr>`).join("")}
    </tbody></table>`;
  return card;
}

function buildPlan(r) {
  const card = document.createElement("div");
  card.className = "card";
  const plans = r.comparison ? Object.entries(r.plan) : [["本次执行", r.plan]];
  card.innerHTML = `<div class="section-title"><h2>执行计划</h2>
    <span class="muted">含源端真实 EXPLAIN QUERY PLAN</span></div>`;
  plans.forEach(([name, plan]) => {
    const wrap = document.createElement("details");
    wrap.open = true;
    const steps = (plan.steps || []).map((st) => `
      <li><span class="step-src">${escapeHtml(st.source)}</span>
        <b>步骤 ${st.step}</b> · ${escapeHtml(st.action)}
        ${st.pushed_down_filters?.length ? `<div class="muted">下推：${st.pushed_down_filters.map(escapeHtml).join("、")}</div>` : ""}
        ${st.applied_filters ? `<div class="muted">应用层筛选：${Object.entries(st.applied_filters).map(([k, v]) => `${k}: ${v.join("、")}`).join("；")}</div>` : ""}
      </li>`).join("");
    const explain = plan.explain_query_plan ? Object.entries(plan.explain_query_plan)
      .map(([src, lines]) => `<details><summary>EXPLAIN QUERY PLAN · ${escapeHtml(src)}</summary>
        <div class="explain-block">${lines.map((l) => escapeHtml(l.detail)).join("\n")}</div></details>`).join("") : "";
    const plannedSql = plan.planned_sql ? `<details><summary>计划 SQL 模板</summary>
      <div class="sql-block">${escapeHtml(JSON.stringify(plan.planned_sql, null, 2))}</div></details>` : "";
    wrap.innerHTML = `<summary>${escapeHtml(name)} · ${escapeHtml(plan.strategy || "")} — ${escapeHtml(plan.description || "")}</summary>
      <ol class="plan-steps">${steps}</ol>${explain}${plannedSql}`;
    card.appendChild(wrap);
  });
  return card;
}

function buildDetail(r, runId) {
  const card = document.createElement("div");
  card.className = "card";
  const rows = r.detail_preview || [];
  const cols = rows.length ? Object.keys(rows[0]) : [];
  const tableHtml = rows.length
    ? `<div class="table-wrap" style="margin-top:.6rem">
        <table><thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${cols.map((c) =>
          `<td${typeof row[c] === "number" ? ' class="num"' : ""}>${row[c] === null ? "<span class='muted'>NULL</span>" : escapeHtml(String(row[c]))}</td>`).join("")}</tr>`).join("")}
        </tbody></table>
      </div>`
    : `<div class="banner warn" style="margin-top:.6rem">本次查询无匹配明细行（0 行是合法的正式结果，仍可下载仅含 BOM 的 CSV）。</div>`;
  card.innerHTML = `
    <div class="section-title"><h2>最终明细</h2>
      <span class="muted">预览前 ${rows.length} 行 / 共 ${r.detail_total_rows} 行${r.detail_truncated ? "（表格已截断，完整数据请下载）" : ""}</span></div>
    <div class="download-row">
      <a class="btn btn-primary" href="/api/runs/${runId}/download" download>⬇ 下载正式结果 CSV</a>
      <span class="muted">仅整次 completed 的运行可下载；失败/取消访问该地址会被拒绝</span>
    </div>
    ${tableHtml}`;
  return card;
}

function buildRequestCard(req) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<h2>本次请求参数</h2><div class="sql-block">${escapeHtml(JSON.stringify(req, null, 2))}</div>`;
  return card;
}

// ---------------------------------------------------------------- 工具

function el(tag, cls, html) {
  const d = document.createElement(tag);
  d.className = cls;
  d.innerHTML = html;
  return d;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtNum(n) { return Number(n).toLocaleString("zh-CN"); }
function fmtMoney(n) { return "¥" + Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function pct(v, max) { return max ? (v / max * 100).toFixed(1) + "%" : "0%"; }

init();
