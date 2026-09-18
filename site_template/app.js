// Astra 收盘观察 · 前端渲染（零依赖：原生 JS + 内联 SVG）
// 数据契约见 pipeline/build.py build_data_for_site v2
"use strict";

let DATA = null;

async function open_() {
  const pwd = document.getElementById("pwd").value;
  const err = document.getElementById("err");
  err.textContent = "解密中…";
  try {
    DATA = await AStocker.open(pwd);
    err.textContent = "";
    render();
  } catch (e) {
    err.textContent = "口令错误或密文损坏";
  }
}
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && document.getElementById("gate").style.display !== "none") open_();
});

const STATUS_CLS = {
  "条件满足": "b-green", "等待确认": "b-yellow", "数据不足": "b-grey",
  "超价取消": "b-red", "结构失效": "b-red", "到期失效": "b-grey",
};
const ACTION_CLS = {
  "现在买": "b-green", "次日竞价达标买": "b-yellow", "等回踩": "b-yellow",
  "小仓试": "b-yellow", "观望": "b-grey", "禁买": "b-red", "未推荐": "b-grey",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
}

// ---------------------------------------------------------------------------
// 网页自助加自选（2026-09-15 / v2）
// 用户诉求原话：「我能够在网络上单独添加自选的版本」「不要命令行」
//   「我自己会进行添加删除」。
//
// v1 曾让浏览器端用 libsodium 做 crypto_box_seal 再直写 GitHub Secret ——
// 实测不可行：npm 的 libsodium-wrappers 只有 CommonJS 形态，裸 <script>
// 引不进页面；手工拼接 wasm 版又报 base64 解码失败。把服务端加密职责塞进
// 前端本身就脆弱。
//
// v2（本版）：浏览器**只发一个 workflow_dispatch**，把自选清单当文本 input
// 传给 Actions，由 CI 侧 Python PyNaCl 写 Secret（CI 环境 100% 可靠）。
// 浏览器零加密依赖、零第三方库 —— 只需一次普通 fetch。
//   ① POST /repos/{repo}/actions/workflows/{wf}/dispatches
//        {"ref":"main","inputs":{"task":"watch-sync","codes":"sh600519,sz000001"}}
//   ② CI 跑 pipeline.sync_watch 写 Secret WATCH_CODES
//   ③ 下一交易时点 fetch/build 即读到新自选
// 令牌只来自 owner 密文包（DATA._admin.token），不落明文页面、不入库。
// ---------------------------------------------------------------------------
const WATCH_ADMIN = { codes: [], busy: false, msg: "", ok: true };

function _normCode(raw) {
  let s = String(raw || "").trim().toLowerCase().replace(/\s+/g, "");
  if (/^(sh|sz)\d{6}$/.test(s)) return s;
  if (/^\d{6}$/.test(s)) return (s[0] === "6" ? "sh" : "sz") + s;
  return null;
}

async function _ghApi(path, opts) {
  const adm = DATA._admin || {};
  const r = await fetch("https://api.github.com" + path, {
    ...opts,
    headers: {
      "Authorization": "Bearer " + adm.token,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(opts && opts.headers ? opts.headers : {}),
    },
  });
  if (!r.ok) {
    let d = "";
    try { d = (await r.json()).message || ""; } catch (e) { /* 忽略 */ }
    throw new Error(`GitHub ${r.status}${d ? "：" + d : ""}`);
  }
  return r.status === 204 ? {} : r.json();
}

async function _pushWatchToCloud(codes) {
  const adm = DATA._admin || {};
  const wf = adm.workflow || "stock.yml";
  const repo = adm.repo || "aprildream24/astock-system";
  // 触发 CI 的 watch-sync 任务；codes 走 input，CI 侧写 Secret
  await _ghApi(`/repos/${repo}/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      ref: adm.ref || "main",
      inputs: {task: "watch-sync", codes: codes.join(",")},
    }),
  });
}

async function watchAdminAct(fn) {
  if (WATCH_ADMIN.busy) return;
  WATCH_ADMIN.busy = true;
  WATCH_ADMIN.msg = "同步中…";
  watchAdminPaint();
  try {
    await fn();
    WATCH_ADMIN.ok = true;
    WATCH_ADMIN.msg = "已提交云端，1-2 分钟内写入，下个交易时点生效";
  } catch (e) {
    WATCH_ADMIN.ok = false;
    WATCH_ADMIN.msg = "失败：" + (e.message || e);
  }
  WATCH_ADMIN.busy = false;
  watchAdminPaint();
}

function watchAdminPaint() {
  const box = document.getElementById("wadm");
  if (!box) return;
  const list = WATCH_ADMIN.codes;
  box.innerHTML = `
    <div class="row" style="display:flex;gap:8px;margin-bottom:8px">
      <input id="wadm-in" placeholder="股票代码，如 600519" style="flex:1">
      <button id="wadm-add">加入</button>
    </div>
    ${list.length ? `<table>${list.map(c => `<tr>
        <td><span class="tag">${esc(c)}</span></td>
        <td style="text-align:right"><button class="wadm-del" data-c="${esc(c)}"
            style="background:#2c3440;padding:4px 12px;font-size:13px">删除</button></td>
      </tr>`).join("")}</table>`
      : `<div class="empty">暂无自选，输入代码添加</div>`}
    <div class="small ${WATCH_ADMIN.ok ? "muted" : "up"}"
         style="margin-top:8px;min-height:18px">${esc(WATCH_ADMIN.msg)}</div>`;
  const inp = document.getElementById("wadm-in");
  const add = () => {
    const c = _normCode(inp.value);
    if (!c) { WATCH_ADMIN.ok = false; WATCH_ADMIN.msg = "代码格式不对（6位数字）";
              watchAdminPaint(); return; }
    if (WATCH_ADMIN.codes.includes(c)) {
      WATCH_ADMIN.ok = false; WATCH_ADMIN.msg = c + " 已在自选中";
      watchAdminPaint(); return;
    }
    WATCH_ADMIN.codes = WATCH_ADMIN.codes.concat([c]);
    watchAdminAct(() => _pushWatchToCloud(WATCH_ADMIN.codes));
  };
  document.getElementById("wadm-add").onclick = add;
  inp.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
  box.querySelectorAll(".wadm-del").forEach(b => {
    b.onclick = () => {
      WATCH_ADMIN.codes = WATCH_ADMIN.codes.filter(x => x !== b.dataset.c);
      watchAdminAct(() => _pushWatchToCloud(WATCH_ADMIN.codes));
    };
  });
}

// ---------------------------------------------------------------------------
// 持仓管理（用户 2026-09-19「网上添加和修改购买股」）：镜像自选管理范式。
// 浏览器只发一个 workflow_dispatch(task=holdings-sync, holdings=JSON)，
// CI 侧 pipeline.sync_holdings 用 PyNaCl 写 Secret HOLDINGS_CONF，
// 下一个交易时点 build/intraday 自动读到新持仓。零前端加密、零后端。
// ---------------------------------------------------------------------------
const HOLD_ADMIN = { rows: [], busy: false, msg: "", ok: true };

function _pushHoldingsToCloud(rows) {
  const adm = DATA._admin || {};
  const wf = adm.workflow || "stock.yml";
  const repo = adm.repo || "aprildream24/astock-system";
  return _ghApi(`/repos/${repo}/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      ref: adm.ref || "main",
      inputs: {task: "holdings-sync", holdings: JSON.stringify(rows)},
    }),
  });
}

async function holdingAdminAct(fn) {
  if (HOLD_ADMIN.busy) return;
  HOLD_ADMIN.busy = true;
  HOLD_ADMIN.msg = "同步中…";
  holdingAdminPaint();
  try {
    await fn();
    HOLD_ADMIN.ok = true;
    HOLD_ADMIN.msg = "已提交云端，1-2 分钟内写入，下个交易时点生效";
  } catch (e) {
    HOLD_ADMIN.ok = false;
    HOLD_ADMIN.msg = "失败：" + (e.message || e);
  }
  HOLD_ADMIN.busy = false;
  holdingAdminPaint();
}

function holdingAdminPaint() {
  const box = document.getElementById("hadm");
  if (!box) return;
  const rows = HOLD_ADMIN.rows;
  box.innerHTML = `
    <div class="small muted" style="margin-bottom:6px">
      每行一只：代码 / 买入价 / 股数（可留空）。保存后整表覆盖云端持仓。</div>
    ${rows.length ? `<table>${rows.map((r, i) => `<tr>
        <td><span class="tag">${esc(r.code)}</span></td>
        <td><input class="h-buy" data-i="${i}" value="${esc(r.buy_price ?? "")}"
            placeholder="买入价" style="width:82px"></td>
        <td><input class="h-sh" data-i="${i}" value="${esc(r.shares ?? "")}"
            placeholder="股数" style="width:72px"></td>
        <td style="text-align:right"><button class="h-del" data-i="${i}"
            style="background:#2c3440;padding:4px 12px;font-size:13px">删除</button></td>
      </tr>`).join("")}</table>`
      : `<div class="empty">暂无持仓登记</div>`}
    <div class="row" style="display:flex;gap:8px;margin-top:8px">
      <input id="hadm-in" placeholder="股票代码，如 002493" style="flex:1">
      <button id="hadm-add">买入登记</button>
    </div>
    <div class="small ${HOLD_ADMIN.ok ? "muted" : "up"}"
         style="margin-top:8px;min-height:18px">${esc(HOLD_ADMIN.msg)}</div>`;
  const inp = document.getElementById("hadm-in");
  const add = () => {
    const c = _normCode(inp.value);
    if (!c) { HOLD_ADMIN.ok = false; HOLD_ADMIN.msg = "代码格式不对（6位数字）";
              holdingAdminPaint(); return; }
    if (rows.some(r => r.code === c)) {
      HOLD_ADMIN.ok = false; HOLD_ADMIN.msg = c + " 已在持仓中";
      holdingAdminPaint(); return;
    }
    rows.push({code: c, name: "", buy_price: null, shares: null,
               buy_date: new Date().toISOString().slice(0, 10)});
    inp.value = "";
    HOLD_ADMIN.msg = "填好买入价后点保存";
    holdingAdminPaint();
  };
  document.getElementById("hadm-add").onclick = add;
  inp.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
  box.querySelectorAll(".h-del").forEach(b => {
    b.onclick = () => {
      rows.splice(Number(b.dataset.i), 1);
      holdingAdminAct(() => _pushHoldingsToCloud(rows));
    };
  });
  box.querySelectorAll(".h-buy,.h-sh").forEach(el => {
    el.onchange = () => {
      const i = Number(el.dataset.i);
      rows[i].buy_price = parseFloat(el.classList.contains("h-buy")
        ? el.value : rows[i].buy_price) || null;
      rows[i].shares = parseFloat(el.classList.contains("h-sh")
        ? el.value : rows[i].shares) || null;
    };
  });
  const save = document.getElementById("hadm-save");
  if (save) save.onclick = () => {
    HOLD_ADMIN.rows = rows.filter(r => r.buy_price);   // 没填价的行不保存
    holdingAdminAct(() => _pushHoldingsToCloud(HOLD_ADMIN.rows));
  };
}

function holdingManageCard() {
  const adm = DATA._admin;
  if (!adm) return "";                        // 非 owner：整块不渲染
  HOLD_ADMIN.rows = (DATA.holdings_detail || []).map(h => ({
    code: h.code, name: h.name || "", buy_price: h.buy_price,
    shares: h.shares, buy_date: h.buy_date || h.buy_date === null ? h.buy_date
      : new Date().toISOString().slice(0, 10)}));
  if (!adm.enabled) {
    return `<div class="card"><h3>持仓管理</h3>
      <div class="small muted">未配置写入令牌（SITE_EDIT_TOKEN），
      当前仅可查看。在仓库 Secrets 加上该令牌后即可在此直接登记/修改持仓。</div>
    </div>`;
  }
  setTimeout(holdingAdminPaint, 0);
  return `<div class="card">
    <h3>持仓管理 · 网页直接登记/修改</h3>
    <div class="small muted" style="margin-bottom:10px">
      改动直接写入云端（${esc(adm.repo)}），下个交易时点起
      持仓体检/卖出信号按新持仓计算。</div>
    <div id="hadm"></div>
    <div style="margin-top:10px"><button id="hadm-save"
        style="background:#1d7a4c">保存持仓到云端</button></div>
  </div>`;
}

function watchManageCard() {
  const adm = DATA._admin;
  if (!adm) return "";                        // 非 owner：整块不渲染
  WATCH_ADMIN.codes = (DATA.watch_advice || [])
    .map(w => w.code).filter(Boolean);
  if (!adm.enabled) {
    return `<div class="card"><h3>自选股管理</h3>
      <div class="small muted">未配置写入令牌（SITE_EDIT_TOKEN），
      当前仅可查看。在仓库 Secrets 加上该令牌后即可在此直接增删自选。</div>
    </div>`;
  }
  setTimeout(watchAdminPaint, 0);
  return `<div class="card">
    <h3>自选股管理 · 网页直接增删</h3>
    <div class="small muted" style="margin-bottom:10px">
      改动直接写入云端（${esc(adm.repo)}），下一个交易时点自动生效，
      无需命令行、无需本机开机。</div>
    <div id="wadm"></div>
  </div>`;
}

function render() {
  document.getElementById("gate").style.display = "none";
  const app = document.getElementById("app");
  app.style.display = "block";
  const views = ["overview", "signals", "curve", "detail"];
  const names = {overview: "概览", signals: "观察池", curve: "胜率", detail: "明细"};
  app.innerHTML = `
    <header><h1>Astra 收盘观察</h1><span class="date">${esc(DATA.date)}</span></header>
    <nav id="nav">${views.map((v, i) =>
      `<button data-v="${v}" class="${i === 0 ? "on" : ""}">${names[v]}</button>`).join("")}</nav>
    <div id="view"></div>
    <div class="footer">
      ${esc(DATA.meta?.note || "")}<br>
      ${esc(DATA.meta?.disclosure || "")}<br>
      规则版本 ${esc(DATA.meta?.rule_version || "-")} · 复核 ${esc(DATA.meta?.reviewed ?? "-")} 只 ·
      有效期至 ${esc(DATA.meta?.valid_until || "-")}
    </div>`;
  document.getElementById("nav").addEventListener("click", e => {
    if (e.target.dataset.v) {
      document.querySelectorAll("#nav button").forEach(b => b.classList.remove("on"));
      e.target.classList.add("on");
      document.getElementById("view").innerHTML = VIEWS[e.target.dataset.v]();
    }
  });
  document.getElementById("view").innerHTML = VIEWS.overview();
}

const VIEWS = {
  overview() {
    return emoCard() + watchManageCard() + holdingManageCard() + planCards() + watchCard()
      + changeCard() + triggerCard() + banner();
  },
  signals() {
    const sigs = DATA.signals || [];
    if (!sigs.length) return emptyBox("暂无观察信号");
    const rows = sigs.map(s => {
      const z = s.zone || [];
      const nm = nameOf(s.code);
      return `<tr>
        <td><span class="badge ${STATUS_CLS[s.status] || "b-grey"}">${esc(s.status)}</span><br>
            <b>${esc(nm || "—")}</b>${nm ? `<span class="code muted"> ${esc(s.code)}</span>`
            : `<span class="code muted">${esc(s.code)}</span>`}</td>
        <td class="zone">${z[0] != null ? z[0].toFixed(2) : "—"} ~ ${z[1] != null ? z[1].toFixed(2) : "—"}<br>
            <span class="muted small">止损 ${s.stop != null ? s.stop.toFixed(2) : "—"}</span></td>
        <td class="small muted">${esc(s.reason || "—")}</td></tr>`;
    }).join("");
    return `<div class="card"><h3>观察池生命周期（等待确认 → 条件满足 / 失效）</h3>
      <table><tr><th>标的/状态</th><th>关注区间</th><th>入选理由</th></tr>${rows}</table></div>`;
  },
  curve() {
    const rp = DATA.recperf;
    if (!rp) return emptyBox("胜率曲线：暂无足够历史样本（T+2 结局回填累计中）");
    const r30 = rp.recent30 || {};
    return `
    <div class="card"><h3>推荐池 T+2 等权净值</h3>
      ${lineChart(rp.dates, rp.cumulative, "#5b8def")}
      <div class="meta-row"><span>期末净值 <b class="${rp.final_cum >= 1 ? "up" : "down"}">${rp.final_cum}</b></span>
      <span>回溯 ${rp.n_days} 个交易日</span>
      <span>近30日胜率 <b>${r30.win_rate ?? "—"}%</b></span>
      <span>近30日均值 <b class="${(r30.avg_pct || 0) >= 0 ? "up" : "down"}">${r30.avg_pct ?? "—"}%</b></span></div>
    </div>
    <div class="card"><h3>分阶段胜率（当日均值 vs 20日滚动）</h3>
      ${Object.entries(rp.phase_winrate || {}).map(([ph, v]) =>
        `<div class="kv"><span class="k">${esc(ph)}（${v.n_days} 日）</span>
         <span class="${v.win_rate >= 50 ? "up" : "down"}">${v.win_rate}%</span></div>`).join("")}
    </div>`;
  },
  detail() {
    const cands = DATA.candidates || [];
    const skipped = DATA.skipped || [];
    const x = DATA.xcheck || {};
    let h = "";
    h += `<div class="card"><h3>全部候选（${cands.length}）</h3>` +
      (cands.length ? `<table><tr><th>标的</th><th>动作</th><th>区间</th><th>属性</th></tr>` +
        cands.map(c => {
          const ex = c.extra || {};
          return `<tr><td><b>${esc(c.name || "—")}</b><span class="code muted"> ${esc(c.code)}</span><br>
              <span class="tag">${esc(c.pool)}</span><span class="score-tag">${c.score ?? "—"}</span></td>
            <td><span class="badge ${ACTION_CLS[c.action] || "b-grey"}">${esc(c.action)}</span></td>
            <td class="zone small">${ex.buy_low != null ? ex.buy_low.toFixed(2) + "~" + ex.buy_high.toFixed(2) : "—"}</td>
            <td class="small muted">${[ex.speed && ex.speed !== "常规" ? ex.speed + ex.hold_days + "日" : "",
              ex.consecutive_limit_ups ? ex.consecutive_limit_ups + "板" : ""]
              .filter(Boolean).join(" · ") || "—"}</td></tr>`;
        }).join("") + `</table>` : `<div class="empty">今日无候选</div>`) + `</div>`;
    h += `<div class="card"><h3>未入选原因（${skipped.length}）</h3>` +
      (skipped.length ? skipped.slice(0, 40).map(s =>
        `<div class="kv"><span class="k">${esc(s.code)}</span><span>${esc(
          (s.extra && s.extra.reason) || s.action || "—")}</span></div>`).join("")
        : `<div class="empty">无</div>`) + `</div>`;
    if (x && x.checked) {
      h += `<div class="card"><h3>三源交叉抽查（东财/新浪/腾讯）</h3>
        <div class="kv"><span class="k">抽查数量</span><span>${x.checked}（有数据 ${x.with_data}）</span></div>
        <div class="kv"><span class="k">价差>0.5% 存疑</span><span class="${x.flagged_count ? "down" : ""}">${x.flagged_count}</span></div>
        ${(x.flagged || []).slice(0, 5).map(f =>
          `<div class="kv"><span class="k">${esc(f.code)}</span><span class="small">价差 ${f.spread_pct}%</span></div>`).join("")}
      </div>`;
    }
    return h;
  },
};

function emptyBox(t) {
  return `<div class="card"><div class="empty">${esc(t)}</div></div>`;
}

function nameOf(code) {
  const c = (DATA.candidates || []).find(x => x.code === code);
  if (c && c.name) return c.name;
  const s = (DATA.signals || []).find(x => x.code === code);
  return (s && s.name) || "";
}

function banner() {
  const rl = DATA.risk_levels;
  if (!rl || !rl.overall) return "";
  const m = {"red": ["red", "红·立即行动"], "yellow": ["yellow", "黄·提高警惕"],
             "blue": ["blue", "蓝·正常跟踪"]}[rl.overall.level] || ["blue", "正常"];
  const rs = (rl.overall.reasons || []).map(r => `<div class="small">${esc(r)}</div>`).join("");
  return `<div class="banner ${m[0]}">${m[1]}${rs ? "<br>" + rs : ""}</div>`;
}

function emoCard() {
  const e = DATA.emotion;
  if (!e) return emptyBox("情绪数据未生成");
  const ang = -Math.PI / 2 + Math.PI * (e.score / 100);
  const cx = 70, cy = 66, r = 52;
  const x2 = cx + r * Math.cos(ang), y2 = cy + r * Math.sin(ang);
  const parts = Object.entries(e.parts || {}).map(([k, v]) =>
    `<div class="dim"><span>${esc(v && v.name || k)}</span><span class="${v && v.ok ? "ok" : "miss"}">${
      v ? (v.ok ? v.score : "缺失") : "缺失"}</span></div>`).join("");
  return `<div class="card"><h3>市场情绪温度计</h3>
    <div class="emo">
      <svg width="140" height="86" viewBox="0 0 140 86">
        <path d="M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}"
              fill="none" stroke="#2c3440" stroke-width="9" stroke-linecap="round"/>
        <path d="M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${x2} ${y2}"
              fill="none" stroke="${e.score >= 60 ? "#ff6b6b" : e.score >= 45 ? "#5b8def" : "#3ddc84"}"
              stroke-width="9" stroke-linecap="round"/>
        <text x="${cx}" y="${cy - 6}" text-anchor="middle" fill="#e8eaf0"
              font-size="22" font-weight="800">${e.score}</text>
        <text x="${cx}" y="${cy + 12}" text-anchor="middle" fill="#8b95a5" font-size="11">/100</text>
      </svg>
      <div class="info">
        <div class="score" style="font-size:18px">${esc(e.label)} · ${esc(e.phase)}</div>
        <div class="lbl small">十维有效 ${e.effective}/10 · 权重覆盖 ${(e.coverage * 100).toFixed(0)}%
          ${e.qualified ? "" : " · <b style='color:var(--warn)'>未达标——不用于策略加权</b>"}</div>
      </div>
    </div>
    <div class="dimgrid">${parts}</div>
  </div>`;
}

function planCards() {
  const NOW = ["现在买", "等回踩", "小仓试"];
  const cands = (DATA.candidates || [])
    .filter(c => NOW.includes(c.action))
    .sort((a, b) => (b.score || 0) - (a.score || 0));
  const mk = (c, first) => {
    const ex = c.extra || {};
    const zone = (ex.buy_low != null && ex.buy_high != null)
      ? `${ex.buy_low.toFixed(2)} ~ ${ex.buy_high.toFixed(2)}` : "—";
    const cap = ex.buy_high != null ? (ex.buy_high * 1.03).toFixed(2) : "—";
    return `<div class="stock-card">
      <div class="top"><span><span class="name">${esc(c.name || c.code)}</span>
        <span class="code">${esc(c.code)}</span></span>
        <span class="badge ${ACTION_CLS[c.action] || "b-grey"}">${esc(c.action)}</span></div>
      ${first ? `<div class="small" style="color:var(--acc);font-weight:700;margin-bottom:4px">【首选观察】</div>` : ""}
      <div class="kv"><span class="k">关注区间</span><span class="zone">${zone}</span></div>
      <div class="kv"><span class="k">不追价上限</span><span class="zone">${cap}</span></div>
      <div class="kv"><span class="k">止损</span><span class="zone">${ex.stop != null ? ex.stop.toFixed(2) : "—"}</span></div>
      <div class="kv"><span class="k">有效期至</span><span>${esc(DATA.meta?.valid_until || "—")}</span></div>
      ${ex.cycle_hint && ex.speed !== "常规" ? `<div class="kv"><span class="k">节奏</span><span>${esc(ex.cycle_hint)}（${ex.hold_days}日）</span></div>` : ""}
      <div class="reason">理由：${esc(ex.entry_hint || ex.cycle_hint || "引擎规则命中")}
        <span class="score-tag">｜${esc(c.pool)} ${c.score ?? ""}</span></div>
    </div>`;
  };
  let h = "";
  if (!cands.length)
    h += emptyBox("今日无当下可买入的机会——没有机会就不凑数。");
  else
    h += mk(cands[0], true) + cands.slice(1, 3).map(c => mk(c, false)).join("");
  // 次日竞价确认通道（当日涨停买不进 → 非即时可买，单独分组）
  const ladder = DATA.ladder_next || [];
  if (ladder.length) {
    h += `<div class="card"><h3>次日竞价确认 · 非即时可买（当日已涨停）</h3>` +
      ladder.map(c => {
        const ex = c.extra || {};
        return `<div class="stock-card">
          <div class="top"><span><span class="name">${esc(c.name || c.code)}</span>
            <span class="code">${esc(c.code)}</span></span>
            <span class="badge b-yellow">🎯竞价达标买</span></div>
          <div class="kv"><span class="k">达标条件</span><span>高开≥2%~5%（按板数）</span></div>
          <div class="kv"><span class="k">低开处理</span><span>放弃（历史胜率仅24%）</span></div>
          <div class="kv"><span class="k">连板高度</span><span>${ex.consecutive_limit_ups || "—"} 板</span></div>
        </div>`;
      }).join("") + `</div>`;
  }
  return h;
}

function watchCard() {
  const w = DATA.watch_advice || [];
  if (!w.length) return "";
  const cls = {"可买（回落至买区）": "b-green", "微超": "b-yellow",
               "等回踩": "b-yellow", "过热": "b-red", "已破位": "b-red",
               "已涨停": "b-grey", "数据不足": "b-grey"};
  return `<div class="card"><h3>⭐ 自选股操作建议（${w.length}）</h3>` +
    w.map(a => {
      const d = a.dist_pct != null ? `｜距买区 ${a.dist_pct > 0 ? "+" : ""}${a.dist_pct}%` : "";
      const dp = a.day_pct != null
        ? `<span class="${a.day_pct >= 0 ? "up" : "down"}">${a.day_pct > 0 ? "+" : ""}${a.day_pct}%</span>` : "";
      return `<div class="stock-card">
        <div class="top"><span><span class="name">${esc(a.name || a.code)}</span>
          <span class="code">${esc(a.code)}</span> ${dp}</span>
          <span class="badge ${cls[a.action] || "b-grey"}">${esc(a.action)}</span></div>
        <div class="kv"><span class="k">建议</span><span>${esc(a.advice)}</span></div>
        ${a.zone ? `<div class="kv"><span class="k">关注区间</span><span class="zone">${a.zone[0]} ~ ${a.zone[1]}</span></div>` : ""}
        ${a.stop ? `<div class="kv"><span class="k">止损</span><span class="zone">${a.stop}</span></div>` : ""}
        ${(a.reasons || []).length ? `<div class="reason">理由：${esc(a.reasons.join(" · "))}</div>` : ""}
      </div>`;
    }).join("") + `</div>`;
}

function changeCard() {
  const ch = DATA.changes || [];
  if (!ch.length) return "";
  return `<div class="card"><h3>计划变化（${ch.length}）</h3>` +
    ch.slice(0, 10).map(c =>
      `<div class="kv"><span class="k">${esc(nameOf(c.code))} ${esc(c.code)}</span>
       <span><span class="badge ${STATUS_CLS[c.status] || "b-grey"}">${esc(c.status)}</span>
       ${c.reason ? `<span class="small muted"> ${esc(c.reason)}</span>` : ""}</span></div>`).join("") +
    `</div>`;
}

function triggerCard() {
  const t = DATA.triggers;
  if (!t || !t.hits || !t.hits.length) return "";
  const cls = {"止损": "b-red", "止盈": "b-green", "买点": "b-yellow", "锁定": "b-grey"};
  return `<div class="card"><h3>触发盯盘（${t.n}）</h3>` +
    t.hits.slice(0, 8).map(h =>
      `<div class="kv"><span><span class="badge ${cls[h.type] || "b-grey"}">${esc(h.type)}</span>
       <b>${esc(h.name || h.code)}</b> <span class="muted small">${esc(h.pool)}</span></span>
       <span class="small muted" style="text-align:right;max-width:55%">${esc(h.detail)}</span></div>`).join("") +
    `</div>`;
}

function lineChart(dates, vals, color) {
  if (!vals || vals.length < 2) return `<div class="empty">样本不足</div>`;
  const W = 640, H = 150, P = 26;
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const span = (mx - mn) || 1;
  const pts = vals.map((v, i) =>
    [P + i / (vals.length - 1) * (W - 2 * P), H - P - (v - mn) / span * (H - 2 * P)]);
  const path = pts.map(p => p.map(n => n.toFixed(1)).join(",")).join(" ");
  const baseY = H - P - ((1.0 - mn) / span) * (H - 2 * P);
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line x1="${P}" y1="${baseY.toFixed(1)}" x2="${W - P}" y2="${baseY.toFixed(1)}"
      stroke="#3ddc84" stroke-dasharray="4 4" stroke-width="1" opacity="0.6"/>
    <polyline points="${path}" fill="none" stroke="${color}" stroke-width="2"/>
    <text x="${P}" y="14" fill="#8b95a5" font-size="11">${mx.toFixed(2)}</text>
    <text x="${P}" y="${H - 8}" fill="#8b95a5" font-size="11">${mn.toFixed(2)} · ${esc(dates[0])} → ${esc(dates[dates.length - 1])}</text>
  </svg>`;
}
