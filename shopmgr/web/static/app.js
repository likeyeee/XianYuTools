/* 闲鱼多店铺管理 · 前端（原生 JS，无构建）。所有平台数据均经 esc() 转义后渲染。 */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (name) => `<svg class="ic"><use href="#i-${name}"/></svg>`;
const fmtTs = (ms) => (ms ? new Date(ms).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
const hhmm = (iso) => (iso ? String(iso).slice(11, 16) : "");

const S = { accounts: [], cur: Number(lsGet("curAcc")) || null, timer: null, qr: null, onModalClose: null, accSig: "" };
const TITLES = { dashboard: "概览", accounts: "账号", items: "商品", orders: "订单", im: "消息", refunds: "售后", rates: "评价", tasks: "任务", settings: "设置" };
const TASK_STATUS = { pending: "排队", running: "执行中", success: "成功", dead: "失败" };

function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }

// ------------------------------------------------------------------ 基础
async function api(method, path, body, opts = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, init);
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("；");
    const err = new Error(msg || r.statusText); err.status = r.status; err.data = data;
    if (r.status === 409 && !opts.quiet) refreshAccounts();
    throw err;
  }
  return data;
}

function toast(msg, isErr) {
  const d = document.createElement("div");
  if (isErr) d.className = "err";
  d.textContent = msg;
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), isErr ? 5000 : 2500);
}

async function guard(fn, btn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}

function taskResult(t, okMsg) {
  if (t.status === "success") toast(okMsg || "已完成");
  else if (t.status === "dead") toast(t.last_error || "失败", true);
  else toast(`已排队 #${t.id}`);
  return t.status === "success";
}

function closeModal() {
  $("#modal").classList.add("hidden");
  const hook = S.onModalClose; S.onModalClose = null;
  if (hook) hook();
}

function modal(html, onMount) {
  S.onModalClose = null;
  $("#modalBody").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $$("[data-close]", $("#modalBody")).forEach((b) => (b.onclick = closeModal));
  if (onMount) onMount($("#modalBody"), closeModal);
  const first = $("input:not([type=checkbox]), textarea", $("#modalBody"));
  if (first) first.focus();
  return closeModal;
}

function previewImage(src, alt) {
  modal(`<h3>${esc(alt || "商品图片")}</h3>
    <div class="img-preview"><img src="${esc(src)}" alt="${esc(alt)}" referrerpolicy="no-referrer"></div>
    ${actions(`<button data-close>关闭</button>`)}`);
}

function confirmBox(title, text, danger) {
  return new Promise((res) => {
    modal(`<h3>${esc(title)}</h3>${text ? `<p class="muted">${esc(text)}</p>` : ""}
      <div class="modal-actions"><button data-close>取消</button><button class="${danger ? "danger" : "primary"}" id="cfOk">确定</button></div>`,
      (el, close) => { $("#cfOk", el).onclick = () => { close(); res(true); }; $("[data-close]", el).addEventListener("click", () => res(false)); });
  });
}

const actions = (html) => `<div class="modal-actions">${html}</div>`;
const field = (label, input) => `<label class="f"><span>${label}</span>${input}</label>`;
const statusHtml = (a) => `<span class="status st-${esc(a.status)}"><i class="dot"></i>${esc(a.status_text)}</span>`;
// 昵称与头像都来自闲鱼资料接口（mtop.idle.web.user.page.nav），未登录成功前不展示占位名
const accLabel = (a) => (a && a.name) || "未命名账号";
// 头像加载失败（图被删 / 防盗链）时换成内联占位图，避免出现浏览器默认的破图图标
const AV_PH = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" fill="#e8eaf0"/>'
  + '<circle cx="20" cy="15" r="6.5" fill="#b6bcc9"/><path d="M7 36c0-7 5.8-11 13-11s13 4 13 11z" fill="#b6bcc9"/></svg>');
const avatarHtml = (a, cls = "av") => (a && a.avatar
  ? `<img class="${cls}" src="${esc(a.avatar)}" alt="" referrerpolicy="no-referrer" loading="lazy" onerror="this.onerror=null;this.src='${AV_PH}'">`
  : `<i class="${cls} av-ph">${ic("users")}</i>`);
// 商品封面：picUrl 来自 mtop.idle.web.xyh.item.list 的 cardData.picInfo.picUrl；
// 加载失败（图被删/防盗链）时直接移除 img，露出底下的占位图标
const thumbHtml = (url, alt = "", cls = "thumb") => `<span class="${cls}">${ic("box")}${
  url ? `<img src="${esc(url)}" alt="${esc(alt)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}</span>`;
const empty = (t = "暂无数据") => `<div class="empty">${ic("inbox")}<div>${esc(t)}</div></div>`;
const loading = () => `<div class="loading"><span class="spin"></span></div>`;
// 没有头像时按 id 派生一个稳定底色，比统一的灰底单字更容易区分（同一个 id 永远同一个颜色）
const AV_COLORS = ["#5b8ff9", "#00a870", "#d48806", "#7262fd", "#0b7fd4", "#d4380d", "#c41d7f", "#7b5cf0"];
const colorOf = (s) => AV_COLORS[[...String(s || "")].reduce((n, ch) => n + ch.codePointAt(0), 0) % AV_COLORS.length];
const curAcc = () => S.accounts.find((a) => a.id === S.cur);
const card = (title, body, extra = "") => `<div class="card">${title ? `<div class="card-h"><h3>${title}</h3><span class="spacer"></span>${extra}</div>` : ""}${body}</div>`;
const table = (head, rows) => `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;

// ------------------------------------------------------------------ 账号与顶栏
async function refreshAccounts() {
  S.accounts = await api("GET", "/accounts");
  if (!S.accounts.find((a) => a.id === S.cur)) S.cur = S.accounts[0] ? S.accounts[0].id : null;
  const sel = $("#accSel");
  // 账号没变化就不重建下拉框：30 秒一次的无谓重排会让正在操作的人感觉卡
  const sig = JSON.stringify([S.cur, S.accounts.map((a) => [a.id, a.name, a.avatar, a.status])]);
  if (sig !== S.accSig) {
    S.accSig = sig;
    sel.innerHTML = S.accounts.length
      ? S.accounts.map((a) => `<option value="${a.id}" ${a.id === S.cur ? "selected" : ""}>${esc(accLabel(a))}</option>`).join("")
      : `<option value="">无账号</option>`;
  }
  const a = curAcc();
  const av = $("#accAvatar");
  if (av) {
    // 头像 404 时不要留一个空白圆圈：换成内联占位图（`hidden` 属性会被 .acc-avatar 的 display 覆盖）
    if (a && a.avatar) {
      av.onerror = () => { av.onerror = null; av.src = AV_PH; };
      av.src = a.avatar; av.hidden = false;
    } else { av.hidden = true; av.removeAttribute("src"); }
  }
  $("#accDot").className = `dot ${a ? "st-" + a.status : ""}`;
  $("#accDot").title = a ? a.status_text : "";
  renderBanner();
}

function renderBanner() {
  const a = curAcc();
  const route = (location.hash.slice(2) || "dashboard").split("?")[0];
  const scoped = !["dashboard", "accounts", "tasks", "settings"].includes(route);
  let html = "";
  if (scoped && a) {
    if (a.status === "restricted") html = `<div class="alert warn">${ic("alert")}<span>风控暂停至 ${esc(hhmm(a.resume_at))}</span><span class="spacer"></span><button class="sm" id="bnOpen">打开窗口处理</button><button class="sm" id="bnResume">已处理，恢复</button></div>`;
    else if (a.status === "expired" || a.status === "new") html = `<div class="alert bad">${ic("alert")}<span>未登录</span><span class="spacer"></span><button class="sm" id="bnLogin">扫码登录</button></div>`;
    else if (a.status === "disabled") html = `<div class="alert bad">${ic("alert")}<span>账号已停用</span></div>`;
  }
  if (scoped && !a) html = `<div class="alert warn">${ic("alert")}<span>暂无账号</span><span class="spacer"></span><button class="sm" onclick="location.hash='#/accounts'">去添加</button></div>`;
  $("#banner").innerHTML = html;
  const r = $("#bnResume"); if (r) r.onclick = () => guard(async () => { await api("POST", `/accounts/${a.id}/resume`); await refreshAccounts(); render(); }, r);
  const o = $("#bnOpen"); if (o) o.onclick = () => guard(async () => { await api("POST", `/accounts/${a.id}/open`); toast("已打开该账号的浏览器窗口，处理完关闭窗口即可"); }, o);
  const l = $("#bnLogin"); if (l) l.onclick = () => openQrLogin(a.id);
}

async function refreshBadges() {
  try {
    const d = await api("GET", "/dashboard", undefined, { quiet: true });
    const row = d.accounts.find((x) => x.account_id === S.cur) || {};
    $("#bWait").textContent = row.wait_consign || "";
    $("#bUnread").textContent = row.unread || "";
    $("#bRefund").textContent = row.refund_pending || "";
    $("#bDead").textContent = d.todo.dead_tasks || "";
    const q = d.queue || {};
    const busy = (q.pending || 0) + (q.running || 0);
    $("#queueTag").innerHTML = `${ic("bolt")}${busy} 个任务执行中`;
    $("#queueTag").classList.toggle("hidden", !busy);
    return d;
  } catch (e) { return null; }
}

// ------------------------------------------------------------------ 概览
async function pageDashboard(v) {
  const d = await refreshBadges();
  if (!d) { v.innerHTML = empty("加载失败"); return; }
  const t = d.todo;
  const kpi = (n, l, icon, tone, go) => `<div class="kpi" data-go="${go}"><div class="kpi-ic tone-${tone}">${ic(icon)}</div><div><div class="n">${n}</div><div class="l">${l}</div></div></div>`;
  const health = d.health.length ? table(
    `<th>账号</th><th>状态</th><th class="num">24h 调用</th><th class="num">成功率</th><th class="num">风控</th><th></th>`,
    d.health.map((h) => `<tr><td class="title-cell">${esc(h.name)}</td><td>${statusHtml(h)}</td>
      <td class="num">${h.calls_24h}</td>
      <td class="num">${h.success_rate == null ? `<span class="muted">—</span>` : `<span class="rate-bar">${h.success_rate}%<i><b style="width:${h.success_rate}%"></b></i></span>`}</td>
      <td class="num">${h.risk_24h || `<span class="muted">0</span>`}</td>
      <td class="act"><button class="link" data-switch="${h.account_id}">切换</button></td></tr>`).join("")) : empty("暂无账号");
  const sales = d.sales.length ? table(`<th>日期</th><th class="num">订单</th><th class="num">金额</th>`,
    d.sales.map((s) => `<tr><td>${esc(s.day.slice(5))}</td><td class="num">${s.orders}</td><td class="num price">¥${s.amount.toFixed(2)}</td></tr>`).join("")) : empty();
  v.innerHTML = `<div class="kpis">
      ${kpi(t.wait_consign, "待发货", "truck", "brand", "#/orders")}
      ${kpi(t.unread, "未读消息", "chat", "info", "#/im")}
      ${kpi(t.refund_pending, "待处理退款", "undo", "warn", "#/refunds")}
      ${kpi(t.dead_tasks, "失败任务", "alert", "bad", "#/tasks?status=dead")}
    </div>
    <div class="dash-grid">${card("账号健康度", health)}${card("近 7 日销售", sales)}</div>`;
  $$("[data-go]", v).forEach((k) => (k.onclick = () => (location.hash = k.dataset.go)));
  $$("[data-switch]", v).forEach((b) => (b.onclick = () => switchAcc(Number(b.dataset.switch))));
}

// ------------------------------------------------------------------ 账号
function stopQrPoll() {
  if (S.qr) { clearTimeout(S.qr.timer); S.qr = null; }
}

function qrTip(text, cls) {
  const t = $("#qrTip"); if (!t) return;
  t.textContent = text; t.className = `qr-tip${cls ? " " + cls : ""}`;
}

function qrShowReload(on) {
  const b = $("#qrReload"); if (b) b.classList.toggle("hidden", !on);
}

/** 应用内扫码：二维码在弹窗里渲染，状态每 2 秒回轮询一次，登录后昵称头像自动落库。 */
function openQrLogin(accountId, opts = {}) {
  stopQrPoll();
  const state = S.qr = { id: accountId, token: null, timer: null, polls: 0, done: false, isNew: !!opts.isNew };
  modal(`<h3>扫码登录</h3>
      <div class="qr-box" id="qrBox"><div class="loading"><span class="spin"></span></div></div>
      <p class="qr-tip" id="qrTip">正在生成二维码…</p>
      ${actions(`<button data-close>取消</button><button class="primary hidden" id="qrReload">刷新二维码</button>`)}`,
    (el) => {
      $("#qrReload", el).onclick = (e) => guard(() => loadQr(el), e.target);
      loadQr(el);
    });
  // 关闭弹窗（取消 / 右上角 / Esc / 遮罩）时释放服务端会话；新建的临时账号一并清理
  S.onModalClose = () => {
    const s = state;
    stopQrPoll();
    if (s.done) return;
    api("DELETE", `/accounts/${s.id}/login/qr`, undefined, { quiet: true })
      .then(() => { if (s.isNew) render(); }).catch(() => {});
  };
  // 直接关标签页/刷新时用 keepalive 请求兜底，避免留下空账号
  window.addEventListener("pagehide", () => {
    if (!state.done) {
      fetch(`/api/accounts/${state.id}/login/qr`, { method: "DELETE", keepalive: true }).catch(() => {});
    }
  }, { once: true });
}

async function loadQr(el) {
  const s = S.qr; if (!s) return;
  const box = $("#qrBox", el);
  box.innerHTML = `<div class="loading"><span class="spin"></span></div>`;
  qrTip("正在生成二维码…"); qrShowReload(false);
  let r;
  try {
    r = await api("POST", `/accounts/${s.id}/login/qr`);
  } catch (e) {
    box.innerHTML = `<div class="empty">${ic("alert")}<div>二维码生成失败</div></div>`;
    qrTip(e.message, "err"); qrShowReload(true);
    return;
  }
  s.token = r.token; s.polls = 0;
  box.innerHTML = `<img src="${esc(r.qr_data_url)}" alt="闲鱼登录二维码">`;
  qrTip("请用闲鱼 App 扫码，并在手机上确认登录");
  s.timer = setTimeout(qrTick, 2000);
}

async function qrTick() {
  const s = S.qr; if (!s) return;
  s.polls += 1;
  if (s.polls > 150) { qrTip("等待超时，请刷新二维码重试", "err"); qrShowReload(true); return; }
  let r;
  try {
    r = await api("GET", `/accounts/${s.id}/login/qr?token=${encodeURIComponent(s.token)}`, undefined, { quiet: true });
  } catch (e) {
    qrTip(e.message, "err"); qrShowReload(true); return;
  }
  if (r.status === "CONFIRMED") {
    if (r.account && r.account.name) {
      s.done = true;
      stopQrPoll(); S.onModalClose = null;
      closeModal();
      toast(`登录成功：${r.account.name}`);
      S.cur = s.id; lsSet("curAcc", s.id);
      await refreshAccounts();
      render();
      return;
    }
    qrTip(r.message || "登录成功，正在读取闲鱼昵称与头像", "ok");
    s.timer = setTimeout(qrTick, 1500);
    return;
  }
  if (["EXPIRED", "CANCELED", "ERROR"].includes(r.status)) {
    qrTip(r.message || "二维码已失效", "err"); qrShowReload(true); return;
  }
  qrTip(r.message || "等待扫码");
  s.timer = setTimeout(qrTick, 2000);
}

async function pageAccounts(v) {
  await refreshAccounts();
  const rows = S.accounts.map((a) => `<tr>
      <td class="title-cell"><span class="acc-cell">${avatarHtml(a)}<span><span class="acc-name">${esc(accLabel(a))}</span><span class="mono">${esc(a.xy_user_id || "未绑定")}</span></span></span></td>
      <td>${statusHtml(a)}${a.status === "restricted" && a.resume_at ? `<span class="muted small"> · 至 ${esc(hhmm(a.resume_at))}</span>` : ""}</td>
      <td class="muted">${esc(a.last_check_at ? a.last_check_at.replace("T", " ").slice(5, 16) : "—")}</td>
      <td><label class="check"><input type="checkbox" data-ar="${a.id}" ${a.autoreply_enabled ? "checked" : ""}></label></td>
      <td class="act">
        ${a.status === "ok" ? "" : `<button class="sm primary" data-act="login" data-id="${a.id}">扫码登录</button>`}
        ${a.status === "restricted" ? `<button class="sm" data-act="resume" data-id="${a.id}">恢复</button>` : ""}
        <button class="link" data-act="check" data-id="${a.id}">检测</button>
        <button class="link" data-act="open" data-id="${a.id}">窗口</button>
        <button class="link" data-act="${a.status === "disabled" ? "enable" : "disable"}" data-id="${a.id}">${a.status === "disabled" ? "启用" : "停用"}</button>
        <button class="link danger" data-act="del" data-id="${a.id}">删除</button>
      </td></tr>`).join("");
  v.innerHTML = `<div class="toolbar"><span class="muted">${S.accounts.length} 个账号</span><span class="spacer"></span>
      <button id="checkAll">${ic("refresh")}全部检测</button><button class="primary" id="addAcc">${ic("plus")}添加账号</button></div>
    ${card("", S.accounts.length ? table(`<th>账号</th><th>状态</th><th>最后检测</th><th>自动回复</th><th></th>`, rows) : empty("暂无账号"))}`;
  // 不再让使用者起名字：建好账号直接出二维码，昵称头像由闲鱼资料接口回填
  $("#addAcc").onclick = (e) => guard(async () => {
    const a = await api("POST", "/accounts");
    await refreshAccounts();
    render();                       // 列表里先出现待登录的空账号
    openQrLogin(a.id, { isNew: true });
  }, e.target);
  $("#checkAll").onclick = (e) => guard(async () => {
    for (const a of S.accounts) if (a.status !== "disabled") await api("POST", `/accounts/${a.id}/check`, undefined, { quiet: true }).catch(() => {});
    toast("检测完成"); render();
  }, e.target);
  $$("[data-ar]", v).forEach((c) => (c.onchange = () => guard(async () => {
    await api("PATCH", `/accounts/${c.dataset.ar}`, { autoreply_enabled: c.checked }); toast(c.checked ? "自动回复已开启" : "自动回复已关闭");
  })));
  $$("[data-act]", v).forEach((b) => (b.onclick = () => guard(async () => {
    const id = b.dataset.id, act = b.dataset.act;
    if (act === "login") { openQrLogin(id); return; }
    if (act === "check") { const r = await api("POST", `/accounts/${id}/check`); toast(r.ok ? "登录正常" : r.error, !r.ok); }
    if (act === "open") await api("POST", `/accounts/${id}/open`);
    if (act === "resume") await api("POST", `/accounts/${id}/resume`);
    if (act === "disable" || act === "enable") await api("PATCH", `/accounts/${id}`, { disabled: act === "disable" });
    if (act === "del") {
      // 保留 profile：里面是设备身份（cna/sgcookie），删掉会让下次登录被当成新设备
      if (!(await confirmBox("删除账号？", "浏览器设备身份会保留，重新添加同一账号时不会被平台当成新设备", true))) return;
      await api("DELETE", `/accounts/${id}`);
      toast("账号已删除，设备身份已保留");
    }
    render();
  }, b)));
}

// ------------------------------------------------------------------ 商品
function bindSelection(v, onChange) {
  const sel = () => $$(".ck:checked", v);
  const upd = () => onChange(sel());
  $$(".ck", v).forEach((c) => (c.onchange = upd));
  const all = $("#all", v); if (all) all.onchange = () => { $$(".ck", v).forEach((c) => (c.checked = all.checked)); upd(); };
  return sel;
}

async function pageItems(v, q) {
  const a = curAcc(); if (!a) { v.innerHTML = ""; return; }
  const page = Number(q.get("page") || 1), group = q.get("group") || "";
  const d = await api("GET", `/a/${a.id}/items?page=${page}&group=${encodeURIComponent(group)}`);
  const link = (p) => `#/items?page=${p}&group=${encodeURIComponent(group)}`;
  const rows = d.items.map((it) => `<tr>
      <td class="w0"><input type="checkbox" class="ck" value="${esc(it.item_id)}" data-price="${esc(it.price)}"></td>
      <td class="title-cell"><span class="item-cell">${thumbHtml(it.pic_url, it.title)}<span class="item-meta">
        <span class="item-title">${esc(it.title)}</span><span class="mono">${esc(it.item_id)}</span></span></span></td>
      <td class="num price">¥${esc(it.price)}</td>
      <td><span class="pill ${it.status === 0 ? "success" : ""}">${esc(it.status_text)}</span></td>
      <td class="act"><button class="link" data-e="${esc(it.item_id)}">编辑</button>
        ${it.status === 0 ? `<button class="link" data-a="polish" data-i="${esc(it.item_id)}">擦亮</button><button class="link" data-a="cancel" data-i="${esc(it.item_id)}">下架</button>`
                          : `<button class="link" data-a="open" data-i="${esc(it.item_id)}">上架</button>`}</td></tr>`).join("");
  v.innerHTML = `<div class="toolbar">
      <div class="seg">${[{ name: "" }, ...d.groups.filter((g) => g.name !== "综合")].map((g) => `<a href="#/items?group=${encodeURIComponent(g.name)}" class="${g.name === group ? "on" : ""}">${esc(g.name || "全部")}</a>`).join("")}</div>
      <span class="spacer"></span>
      <span class="bulk hidden" id="bulk">已选 <b id="selCnt">0</b>
        <button data-b="open">上架</button><button data-b="cancel">下架</button><button data-b="polish">擦亮</button><button data-b="price">改价</button></span>
      <button class="icon" id="itRefresh" title="刷新">${ic("refresh")}</button>
      <button id="drafts">草稿箱</button><button class="primary" id="pub">${ic("plus")}发布商品</button></div>
    ${card("", d.items.length ? table(`<th class="w0"><input type="checkbox" id="all"></th><th>商品</th><th class="num">价格</th><th>状态</th><th></th>`, rows)
      + `<div class="pager">${page > 1 ? `<a href="${link(page - 1)}">上一页</a>` : ""}<span>第 ${page} 页 · 共 ${d.total} 件</span>${d.has_next ? `<a href="${link(page + 1)}">下一页</a>` : ""}</div>` : empty())}`;
  $("#itRefresh").onclick = () => render();
  $$(".thumb img", v).forEach((img) => (img.onclick = () => previewImage(img.src, img.alt)));
  const selected = bindSelection(v, (s) => { $("#selCnt").textContent = s.length; $("#bulk").classList.toggle("hidden", !s.length); });
  $$("[data-a]", v).forEach((b) => (b.onclick = () => guard(async () => {
    if (taskResult(await api("POST", `/a/${a.id}/items/${b.dataset.i}/action`, { action: b.dataset.a }))) render();
  }, b)));
  $$("[data-b]", v).forEach((b) => (b.onclick = async () => {
    const ids = selected().map((c) => c.value);
    const action = b.dataset.b;
    const go = async (extra = {}) => {
      const r = await api("POST", `/a/${a.id}/items/batch`, { action, item_ids: ids, ...extra });
      toast(`已创建批次 #${r.batch_id}`); location.hash = "#/tasks";
    };
    if (action !== "price") { if (await confirmBox(`批量${b.textContent} ${ids.length} 个商品？`)) guard(go); return; }
    modal(`<h3>批量改价 · ${ids.length} 个</h3>
      <div class="row">${field("方式", `<select id="pm"><option value="fixed">统一价格（元）</option><option value="percent">按原价（%）</option></select>`)}
      ${field("数值", `<input id="pv" type="number" step="0.01" min="0.01">`)}</div>
      ${actions(`<button data-close>取消</button><button class="primary" id="pok">确定</button>`)}`,
      (el, close) => ($("#pok", el).onclick = (e) => guard(async () => {
        const prices = Object.fromEntries(selected().map((c) => [c.value, c.dataset.price]));
        await go({ price_mode: $("#pm", el).value, value: Number($("#pv", el).value), prices }); close();
      }, e.target)));
  }));
  $("#pub").onclick = () => itemForm(a, null);
  $$("[data-e]", v).forEach((b) => (b.onclick = () => guard(async () => itemForm(a, b.dataset.e, await api("GET", `/a/${a.id}/items/${b.dataset.e}/edit`)), b)));
  $("#drafts").onclick = () => draftBox(a);
}

function itemForm(a, itemId, detail) {
  const d = detail || {};
  const imgs = (d.imageInfos || []).map((x) => x.url || x).filter(Boolean);
  modal(`<h3>${itemId ? "编辑商品" : "发布商品"}</h3>
    ${field("标题", `<input id="fT" maxlength="60" value="${esc(d.title || "")}"><div id="bw" class="hint"></div>`)}
    ${field("描述", `<textarea id="fD">${esc(d.desc || "")}</textarea>`)}
    <div class="grid3">${field("价格", `<input id="fP" type="number" step="0.01" min="0.01" value="${esc(d.price || "")}">`)}
      ${field("原价", `<input id="fO" type="number" step="0.01" min="0">`)}
      ${field("库存", `<input id="fQ" type="number" min="1" value="${esc(d.quantity || 1)}">`)}</div>
    ${field("图片", `<input id="fI" type="file" accept="image/*" multiple><div class="thumbs" id="thumbs"></div>`)}
    <div class="row"><button class="sm" id="fProp" type="button">推荐属性</button><div class="thumbs" id="props" style="margin:0"></div></div>
    ${actions(`<button data-close>取消</button>${itemId ? "" : `<button id="fDraft">存草稿</button>`}<button class="primary" id="fOk">${itemId ? "保存" : "发布"}</button>`)}`,
  (el, close) => {
    const images = [...imgs]; let props = [];
    const drawThumbs = () => ($("#thumbs", el).innerHTML = images.map((u, i) => `<span>图 ${i + 1}</span>`).join(""));
    drawThumbs();
    let bwTimer;
    $("#fT", el).oninput = () => { clearTimeout(bwTimer); bwTimer = setTimeout(async () => {
      const title = $("#fT", el).value.trim(); if (!title) { $("#bw", el).innerHTML = ""; return; }
      try { const r = await api("POST", `/a/${a.id}/items/badwords`, { title, desc: $("#fD", el).value });
        $("#bw", el).innerHTML = r.hits.length ? `<span class="hit">违禁词：${r.hits.map(esc).join("、")}</span>` : "";
      } catch (e) { $("#bw", el).innerHTML = ""; }
    }, 600); };
    $("#fI", el).onchange = async (e) => {
      for (const f of e.target.files) {
        const ok = await new Promise((res) => { const im = new Image(); im.onload = () => res(Math.min(im.width, im.height) >= 400); im.onerror = () => res(false); im.src = URL.createObjectURL(f); });
        if (!ok) { toast(`${f.name} 小于 400×400`, true); continue; }
        const fd = new FormData(); fd.append("file", f);
        await guard(async () => { const r = await api("POST", `/a/${a.id}/upload`, fd); images.push(r.url); drawThumbs(); });
      }
      e.target.value = "";
    };
    $("#fProp", el).onclick = (e) => guard(async () => {
      props = await api("POST", `/a/${a.id}/items/props`, { title: $("#fT", el).value.trim() || "商品" });
      $("#props", el).innerHTML = props.map((p) => `<span>${esc(p.name)}：${esc(p.value)}</span>`).join("");
    }, e.target);
    const form = () => ({ title: $("#fT", el).value.trim(), desc: $("#fD", el).value, price: Number($("#fP", el).value),
      orig_price: $("#fO", el).value ? Number($("#fO", el).value) : null, quantity: Number($("#fQ", el).value || 1),
      images, props: props.map((p) => p.cpv) });
    $("#fOk", el).onclick = (e) => guard(async () => {
      const t = itemId ? await api("POST", `/a/${a.id}/items/${itemId}/edit`, form()) : await api("POST", `/a/${a.id}/items/publish`, form());
      if (taskResult(t, itemId ? "已保存" : "已发布")) { close(); render(); }
    }, e.target);
    const dr = $("#fDraft", el);
    if (dr) dr.onclick = (e) => guard(async () => { if (taskResult(await api("POST", `/a/${a.id}/drafts`, { form: form() }), "已存草稿")) close(); }, e.target);
  });
}

async function draftBox(a) {
  const list = await guard(() => api("GET", `/a/${a.id}/drafts`));
  if (!list) return;
  modal(`<h3>草稿箱</h3>${list.length ? table("", list.map((d) => `<tr><td class="title-cell">${esc(d.title || "无标题")}<span class="mono">${esc(d.updated)}</span></td>
    <td class="act"><button class="link" data-p="${esc(d.draft_id)}">发布</button><button class="link danger" data-x="${esc(d.draft_id)}">删除</button></td></tr>`).join("")) : empty("草稿箱为空")}`, (el, close) => {
    $$("[data-p]", el).forEach((b) => (b.onclick = () => guard(async () => { if (taskResult(await api("POST", `/a/${a.id}/drafts/${b.dataset.p}/publish`), "已发布")) { close(); render(); } }, b)));
    $$("[data-x]", el).forEach((b) => (b.onclick = () => guard(async () => { if (taskResult(await api("DELETE", `/a/${a.id}/drafts/${b.dataset.x}`), "已删除")) draftBox(a); }, b)));
  });
}

// ------------------------------------------------------------------ 订单
async function pageOrders(v, q) {
  const a = curAcc(); if (!a) { v.innerHTML = ""; return; }
  const tab = q.get("tab") || "wait";
  const tabs = `<div class="seg"><a href="#/orders?tab=wait" class="${tab === "wait" ? "on" : ""}">待发货</a><a href="#/orders?tab=list" class="${tab === "list" ? "on" : ""}">全部订单</a></div>`;
  if (tab === "wait") {
    const d = await api("GET", `/a/${a.id}/orders/wait-consign`);
    const rows = d.orders.map((o) => `<tr><td class="w0"><input type="checkbox" class="ck" value="${esc(o.order_id)}"></td>
      <td class="title-cell">${esc(o.item_title)}<span class="mono">${esc(o.order_id)}</span></td>
      <td>${esc(o.buyer_nick)}</td><td class="num price">¥${esc(o.amount)}</td><td class="muted">${esc(o.pay_time.slice(5, 16))}</td>
      <td class="act"><button class="link" data-det="${esc(o.order_id)}">详情</button><button class="sm primary" data-ship="${esc(o.order_id)}">发货</button></td></tr>`).join("");
    v.innerHTML = `<div class="toolbar">${tabs}<span class="muted">${d.total} 笔</span><span class="spacer"></span>
        <span class="bulk hidden" id="bulk">已选 <b id="selCnt">0</b><button id="bShip">批量虚拟发货</button></span>
        <button class="icon" id="oRef" title="刷新">${ic("refresh")}</button></div>
      ${card("", d.orders.length ? table(`<th class="w0"><input type="checkbox" id="all"></th><th>商品 / 订单号</th><th>买家</th><th class="num">金额</th><th>付款时间</th><th></th>`, rows) : empty("没有待发货订单"))}`;
    $("#oRef").onclick = () => render();
    const selected = bindSelection(v, (s) => { $("#selCnt").textContent = s.length; $("#bulk").classList.toggle("hidden", !s.length); });
    $$("[data-ship]", v).forEach((b) => (b.onclick = () => shipDialog(a, b.dataset.ship)));
    $$("[data-det]", v).forEach((b) => (b.onclick = () => orderDetail(a, b.dataset.det)));
    $("#bShip").onclick = () => {
      const ids = selected().map((c) => c.value);
      modal(`<h3>批量虚拟发货 · ${ids.length} 笔</h3>${field("交付说明", `<textarea id="bt" maxlength="1000"></textarea>`)}
        ${actions(`<button data-close>取消</button><button class="primary" id="bok">发货</button>`)}`,
        (el, close) => ($("#bok", el).onclick = (e) => guard(async () => {
          const r = await api("POST", `/a/${a.id}/orders/batch-ship`, { order_ids: ids, text: $("#bt", el).value.trim() });
          close(); toast(`已创建批次 #${r.batch_id}`); location.hash = "#/tasks";
        }, e.target)));
    };
  } else {
    const d = await api("GET", `/a/${a.id}/orders`);
    const rows = d.orders.map((o) => `<tr><td class="title-cell">${esc(o.title)}<span class="mono">${esc(o.order_id)}</span></td><td>${esc(o.peer_nick)}</td>
      <td class="num price">¥${esc(o.price)}</td><td><span class="pill">${esc(o.status_text)}</span></td><td class="muted">${esc(o.created.slice(5, 16))}</td>
      <td class="act"><button class="link" data-det="${esc(o.order_id)}">详情</button></td></tr>`).join("");
    // 这个接口是买家视角（平台 PC 端没有卖家订单列表），"对方"是卖家。别让人误以为卖出订单为空。
    v.innerHTML = `<div class="toolbar">${tabs}</div>
      <div class="hint muted">这里是你在闲鱼<strong>购买</strong>的订单（平台 PC 端未开放卖家订单列表）；卖出待发货请看"待发货"标签。</div>
      ${card("", d.orders.length ? table(`<th>商品 / 订单号</th><th>对方</th><th class="num">金额</th><th>状态</th><th>时间</th><th></th>`, rows) : empty())}`;
    $$("[data-det]", v).forEach((b) => (b.onclick = () => orderDetail(a, b.dataset.det)));
  }
}

function shipDialog(a, oid) {
  modal(`<h3>发货</h3><div class="mono" style="margin:-10px 0 16px">${esc(oid)}</div>
    <div class="seg" style="margin-bottom:16px"><a href="#" data-t="d" class="on">无需寄件</a><a href="#" data-t="o">已寄出</a></div>
    <div id="tD">${field("交付说明", `<textarea id="sT" maxlength="1000" placeholder="买家可见"></textarea>`)}</div>
    <div id="tO" class="hidden">${field("运单号", `<input id="sM" maxlength="40"><div id="cp" class="hint muted"></div>`)}</div>
    ${actions(`<button data-close>取消</button><button class="primary" id="sOk">确认发货</button>`)}`, (el, close) => {
    let mode = "d", guess = null;
    $$("[data-t]", el).forEach((t) => (t.onclick = (e) => { e.preventDefault(); mode = t.dataset.t;
      $$("[data-t]", el).forEach((x) => x.classList.toggle("on", x === t)); $("#tD", el).classList.toggle("hidden", mode !== "d"); $("#tO", el).classList.toggle("hidden", mode !== "o"); }));
    $("#sM", el).onchange = async () => {
      const no = $("#sM", el).value.trim(); guess = null; if (no.length < 6) return;
      try { guess = await api("GET", `/a/${a.id}/mailno/guess?mail_no=${encodeURIComponent(no)}`); $("#cp", el).textContent = guess.cp_name || guess.cp_code || ""; }
      catch (e) { $("#cp", el).textContent = ""; }
    };
    $("#sOk", el).onclick = (e) => guard(async () => {
      const t = mode === "d"
        ? await api("POST", `/a/${a.id}/orders/${oid}/ship-dummy`, { text: $("#sT", el).value.trim() })
        : await api("POST", `/a/${a.id}/orders/${oid}/ship-offline`, { mail_no: $("#sM", el).value.trim(), cp_code: guess ? guess.cp_code : "", brand_code: guess ? guess.brand_code : "" });
      if (taskResult(t, "已发货")) { close(); render(); }
    }, e.target);
  });
}

async function orderDetail(a, oid) {
  const d = await guard(() => api("GET", `/a/${a.id}/orders/${oid}`));
  if (!d) return;
  modal(`<h3>${esc(d.status_title || d.status || "订单详情")}</h3>
    <dl class="kv"><dt>订单号</dt><dd class="mono">${esc(d.order_id)}</dd><dt>商品</dt><dd class="mono">${esc(d.item_id)}</dd></dl>
    ${actions(`<button id="dW">浏览器打开</button><button id="dD">延长收货</button><button id="dC" class="danger">关闭订单</button><button id="dP" class="primary">改价</button>`)}`, (el) => {
    $("#dW", el).onclick = () => guard(() => api("POST", `/accounts/${a.id}/open?url=${encodeURIComponent("https://www.goofish.com/order-detail?orderId=" + oid)}`));
    $("#dP", el).onclick = () => modal(`<h3>修改价格</h3><div class="row">${field("价格", `<input id="np" type="number" step="0.01" min="0.01">`)}${field("运费", `<input id="nf" type="number" step="0.01" min="0" value="0">`)}</div>
      ${actions(`<button data-close>取消</button><button class="primary" id="nok">确定</button>`)}`,
      (e2, c2) => ($("#nok", e2).onclick = (ev) => guard(async () => {
        if (taskResult(await api("POST", `/a/${a.id}/orders/${oid}/adjust-price`, { price: Number($("#np", e2).value), post_fee: Number($("#nf", e2).value || 0) }), "已改价")) { c2(); render(); }
      }, ev.target)));
    $("#dC", el).onclick = () => guard(async () => {
      const reasons = await api("GET", `/a/${a.id}/orders/${oid}/close-reasons`);
      modal(`<h3>关闭订单</h3>${field("原因", `<select id="cr">${reasons.map((r) => `<option value="${esc(r.code)}">${esc(r.text)}</option>`).join("")}</select>`)}
        ${actions(`<button data-close>取消</button><button class="danger" id="cok">关闭订单</button>`)}`,
        (e2, c2) => ($("#cok", e2).onclick = (ev) => guard(async () => {
          if (taskResult(await api("POST", `/a/${a.id}/orders/${oid}/close`, { reason: $("#cr", e2).value }), "已关闭")) { c2(); render(); }
        }, ev.target)));
    });
    $("#dD", el).onclick = () => guard(async () => { if (await confirmBox("延长收货 3 天？")) taskResult(await api("POST", `/a/${a.id}/orders/${oid}/delay`, { days: 3 }), "已延长"); });
  });
}

// ------------------------------------------------------------------ 消息
async function pageIm(v) {
  const a = curAcc(); if (!a) { v.innerHTML = ""; return; }
  v.innerHTML = `<div class="chat">
    <div class="conv-list" id="convs">${loading()}</div>
    <div class="msgs"><div class="head" id="mhead"></div><div class="list" id="mlist">${empty("选择会话")}</div>
      <div class="composer"><input id="mtext" maxlength="500" placeholder="输入消息"><button id="mcard" title="发送商品卡片">${ic("box")}</button><button class="primary" id="msend">${ic("send")}发送</button></div></div>
    <div class="side-panel">
      <div><h4>快捷回复<span class="spacer"></span><button class="link" id="qrAdd">${ic("plus")}</button></h4><div id="qrs"></div></div>
      <div><h4>自动回复<span class="spacer"></span><a href="#/settings?tab=rules" class="small">规则</a></h4>
        <span class="pill ${a.autoreply_enabled ? "success" : ""}">${a.autoreply_enabled ? "已开启" : "未开启"}</span></div>
      <div><button class="sm danger" id="mblack" disabled>拉黑对方</button></div>
    </div></div>`;
  let cur = null;
  let convs = [];
  let convSig = "";        // 上次渲染的会话列表指纹：没变就不重排 DOM（卡顿主要来源）
  let msgCursor = 0;
  let msgHasMore = false;

  // 对方头像：会话接口本身不带头像，后端按 peer_id 查 user.page.nav 并缓存；
  // 没拿到之前用昵称首字 + 由 peer_id 派生的稳定底色占位
  const convAv = (c) => {
    const name = c.peer_nick || c.peer_id || "?";
    return `<span class="conv-av" style="--av:${colorOf(c.peer_id)}">${
      c.peer_avatar ? `<img src="${esc(c.peer_avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""
    }<i>${esc(name.slice(0, 1))}</i></span>`;
  };
  // 已读回执只对"我发的最后一条"有意义（对方看没看）；对方发来的未读已经由未读数角标表达，
  // 再标一次"未读"是重复信息。read_status 后端已归一成 1=已读 / 0=未读。
  const readTag = (c) => {
    if (!c.last_sender_id || c.last_sender_id !== a.xy_user_id) return "";
    return c.read_status === 1 ? `<span class="rd ok">已读</span>`
      : c.read_status === 0 ? `<span class="rd">未读</span>` : "";
  };
  // 列表里的类型角标：系统提示不值得标，只标有内容的卡片和视频
  const kindTag = (k) => (k === "card" ? `<span class="tag">卡片</span>`
    : k === "video" ? `<span class="tag">视频</span>` : "");

  const convHtml = (c) => {
    const name = c.peer_nick || c.peer_id;
    return `<div class="conv" data-cid="${esc(c.cid)}">${convAv(c)}<div class="body">
      <div class="t"><span class="nm">${esc(name)}</span><span class="meta">${readTag(c)}${c.unread
        ? `<b class="badge">${c.unread}</b>` : `<span class="muted small">${esc(fmtTs(c.last_time).slice(-5))}</span>`}</span></div>
      <div class="s">${kindTag(c.last_kind)}${esc(c.last_text)}</div>
      <div class="s item">${esc(c.item_title)}</div></div></div>`;
  };

  // 指纹只取会影响显示的字段；一致就跳过重建，避免每次刷新都重排整列
  const convsSig = () => JSON.stringify(convs.map((c) => [c.cid, c.peer_nick, c.peer_avatar,
    c.last_text, c.last_kind, c.unread, c.read_status, c.last_sender_id, c.last_time, c.item_title]));

  const bindConvs = () => {
    const box = $("#convs");
    $$(".conv", box).forEach((el) => (el.onclick = () => {
      cur = convs.find((c) => c.cid === el.dataset.cid);
      openConv();
    }));
    if (cur) $$(".conv", box).forEach((el) => el.classList.toggle("on", el.dataset.cid === cur.cid));
  };

  // 头像/昵称要单独查资料接口，那个接口单个就可能 20 秒超时，后端只能后台慢慢补。
  // 所以首屏先用昵称首字占位立即渲染，补到了再局部替换 —— 不重建整列，不跳滚动条。
  const patchConvs = () => {
    $$(".conv", $("#convs")).forEach((el) => {
      const c = convs.find((x) => x.cid === el.dataset.cid);
      if (!c) return;
      const av = el.querySelector(".conv-av");
      if (av && c.peer_avatar && !av.querySelector("img")) {
        av.insertAdjacentHTML("afterbegin",
          `<img src="${esc(c.peer_avatar)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`);
      }
      const nm = el.querySelector(".nm");
      if (nm && c.peer_nick) nm.textContent = c.peer_nick;
    });
    convSig = convsSig();      // 同步指纹，避免下次轮询又重建一次
  };

  let peerRound = 0;
  const fillPeers = (round) => {
    const miss = convs.filter((c) => c.peer_id && c.peer_id !== "0" && !c.peer_avatar);
    if (!miss.length || round > 3) return;
    setTimeout(async () => {
      const ids = miss.map((c) => c.peer_id).join(",");
      const d = await api("GET", `/a/${a.id}/im/peers?ids=${encodeURIComponent(ids)}`,
        undefined, { quiet: true });
      let hit = 0;
      convs.forEach((c) => {
        const p = d[c.peer_id];
        if (!p) return;
        if (p[1] && !c.peer_avatar) { c.peer_avatar = p[1]; hit += 1; }
        if (p[0]) c.peer_nick = p[0];
      });
      if (hit) patchConvs();
      if (hit < miss.length) fillPeers(round + 1);    // 后端还在补，过几秒再拿一次
    }, round ? 5000 : 2500);
  };

  const loadConvs = async (force) => {
    const d = await api("GET", `/a/${a.id}/im/conversations`, undefined, force ? undefined : { quiet: true });
    convs = d.conversations;
    if (convsSig() !== convSig) {
      convSig = convsSig();
      const box = $("#convs");
      box.innerHTML = convs.length ? convs.map(convHtml).join("") : empty("暂无会话");
      bindConvs();
      peerRound = 0;
      fillPeers(0);
    } else if (peerRound === 0 && convs.some((c) => c.peer_id && c.peer_id !== "0" && !c.peer_avatar)) {
      peerRound = 1;         // 列表没变但有缺头像的，接着补
      fillPeers(1);
    }
  };

  const msgHtml = (m, me) => {
    // 系统提示（contentType=14："你已确认收货，交易成功"）是平台插话，不属于任何一方，
    // 渲染成居中灰字，不能套聊天气泡 —— 否则会被误读成对方说的话。
    if (m.kind === "tip") return `<div class="msys">${esc(m.text)}</div>`;
    const mine = m.sender_id === me;
    if (m.kind === "card" || m.kind === "video" || m.kind === "image") {
      const k = m.card || {};
      const title = k.title || m.text || "卡片消息";
      const noPic = !k.pic;      // 通知卡（ct=25）没图，不要留一个空图位
      return `<div class="bubble mcard ${mine ? "me" : ""}${noPic ? " nopic" : ""}">${k.pic
        ? `<img class="mc-pic" src="${esc(k.pic)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('nopic');this.remove()">` : ""}
        <div class="mc-body"><div class="mc-title">${esc(title)}</div>
        ${k.sub ? `<div class="mc-sub2">${esc(k.sub)}</div>` : ""}
        ${k.price ? `<div class="mc-price">¥${esc(k.price)}</div>` : ""}
        ${k.order_id ? `<div class="mc-sub">订单 ${esc(k.order_id)}</div>` : ""}
        ${k.item_id ? `<div class="mc-sub">商品 ${esc(k.item_id)}</div>` : ""}
        ${k.action ? `<div class="mc-act">${esc(k.action)}</div>` : ""}</div>
        <div class="time">${fmtTs(m.created)}</div></div>`;
    }
    return `<div class="bubble ${mine ? "me" : ""}">${esc(m.text)}<div class="time">${fmtTs(m.created)}</div></div>`;
  };

  const renderMsgs = (msgs, prepend) => {
    const me = a.xy_user_id;
    const list = $("#mlist");
    const html = msgs.map((m) => msgHtml(m, me)).join("");
    if (prepend) {
      const old = list.scrollHeight;
      list.insertAdjacentHTML("afterbegin", html);
      list.scrollTop = list.scrollHeight - old;     // 保持视口不跳
    } else {
      list.innerHTML = `<button class="load-more" id="mMore" ${msgHasMore ? "" : "hidden"}>加载更早的消息</button>` + html;
      list.scrollTop = 1e9;
    }
    const more = $("#mMore", list);
    if (more) more.onclick = (e) => guard(() => loadMore(), e.target);
  };

  const loadMore = async () => {
    if (!cur || !msgHasMore) return;
    const d = await api("GET", `/a/${a.id}/im/messages?cid=${encodeURIComponent(cur.cid)}&cursor=${msgCursor}`);
    if (!d.messages.length) { msgHasMore = false; $("#mMore").hidden = true; return; }
    msgCursor = d.next_cursor;
    msgHasMore = d.has_more;
    renderMsgs(d.messages, true);
    if (!msgHasMore) $("#mMore").hidden = true;
  };

  const openConv = async () => {
    $$(".conv", v).forEach((el) => el.classList.toggle("on", el.dataset.cid === cur.cid));
    $("#mhead").innerHTML = `${esc(cur.peer_nick || cur.peer_id)}<span class="muted small" style="font-weight:400">${esc(cur.item_title)}</span>`;
    $("#mblack").disabled = false;
    const d = await guard(() => api("GET", `/a/${a.id}/im/messages?cid=${encodeURIComponent(cur.cid)}`));
    if (!d) return;
    msgCursor = d.next_cursor;
    msgHasMore = d.has_more;
    renderMsgs(d.messages, false);
    $("#mtext").focus();
    // 打开会话后服务端会把未读清掉，这里同步本地状态，不用整列重排
    const c = convs.find((x) => x.cid === cur.cid);
    if (c && c.unread) {
      c.unread = 0;
      const badge = $(`.conv[data-cid="${CSS.escape(cur.cid)}"] .badge`, v);
      if (badge) badge.outerHTML = `<span class="muted small">${esc(fmtTs(c.last_time).slice(-5))}</span>`;
      convSig = "";      // 下次刷新重新比对
    }
  };

  const send = () => guard(async () => {
    const text = $("#mtext").value.trim(); if (!cur) return toast("请先选择会话", true); if (!text) return;
    if (!taskResult(await api("POST", `/a/${a.id}/im/send`, { cid: cur.cid, peer_id: cur.peer_id, text }), "已发送")) return;
    $("#mtext").value = "";
    // 只追加这一条，不重载整个会话（重载会让长会话卡一下）
    $("#mlist").insertAdjacentHTML("beforeend",
      `<div class="bubble me">${esc(text)}<div class="time">刚刚</div></div>`);
    $("#mlist").scrollTop = 1e9;
    convSig = "";
  }, $("#msend"));
  $("#msend").onclick = send;
  $("#mtext").onkeydown = (e) => { if (e.key === "Enter") send(); };
  $("#mcard").onclick = () => { if (!cur) return toast("请先选择会话", true); if (!cur.item_id) return toast("该会话没有关联商品", true);
    guard(async () => { if (taskResult(await api("POST", `/a/${a.id}/im/card`, { cid: cur.cid, item_id: cur.item_id }), "已发送")) { convSig = ""; openConv(); } }); };
  $("#mblack").onclick = () => guard(async () => {
    if (await confirmBox(`拉黑 ${cur.peer_nick || cur.peer_id}？`, "", true)) taskResult(await api("POST", `/a/${a.id}/im/blacklist`, { cid: cur.cid, block: true }), "已拉黑");
  });
  const loadQr = async () => {
    const d = await api("GET", `/a/${a.id}/im/quickreplies`).catch(() => ({ platform: [], local: [] }));
    $("#qrs").innerHTML = [...d.local.map((p) => `<div class="qr" data-q="${esc(p.text)}"><span>${esc(p.text)}</span><button class="x" data-del="${p.id}">${ic("x")}</button></div>`),
      ...d.platform.map((t) => `<div class="qr" data-q="${esc(t)}"><span>${esc(t)}</span></div>`)].join("") || `<span class="muted small">暂无</span>`;
    $$("[data-q]", v).forEach((el) => (el.onclick = (e) => { if (e.target.closest("[data-del]")) return; $("#mtext").value = el.dataset.q; $("#mtext").focus(); }));
    $$("[data-del]", v).forEach((el) => (el.onclick = () => guard(async () => { await api("DELETE", `/quick-phrases/${el.dataset.del}`); loadQr(); })));
  };
  $("#qrAdd").onclick = () => modal(`<h3>新增快捷短语</h3><textarea id="qt" maxlength="500"></textarea>
    ${actions(`<button data-close>取消</button><button class="primary" id="qok">保存</button>`)}`,
    (el, close) => ($("#qok", el).onclick = () => guard(async () => { await api("POST", "/quick-phrases", { text: $("#qt", el).value.trim() }); close(); loadQr(); })));
  await loadConvs(true);
  loadQr();
}

// ------------------------------------------------------------------ 售后
async function pageRefunds(v) {
  const a = curAcc(); if (!a) { v.innerHTML = ""; return; }
  const d = await api("GET", `/a/${a.id}/refunds`);
  const rows = d.refunds.map((r) => `<tr><td class="title-cell">${esc(r.item_title)}<span class="mono">${esc(r.refund_id)}</span></td>
    <td class="num price">¥${esc(r.amount)}</td><td class="muted">${esc(r.reason)}</td>
    <td><span class="pill ${r.status === 1 ? "warn" : r.status === 5 ? "success" : ""}">${esc(r.status_text)}</span></td>
    <td class="muted">${esc(r.created.slice(5, 16))}</td>
    <td class="act"><button class="link" data-d="${esc(r.refund_id)}" data-o="${esc(r.order_id)}">详情</button>${r.status === 1 ? `<button class="sm primary" data-go="${esc(r.refund_id)}" data-o="${esc(r.order_id)}" title="在浏览器中由本人输入支付密码">去处理</button>` : ""}</td></tr>`).join("");
  v.innerHTML = `<div class="toolbar"><span class="muted">${d.refunds.length} 条</span><span class="spacer"></span><button class="icon" id="rRef" title="刷新">${ic("refresh")}</button></div>
    ${card("", d.refunds.length ? table(`<th>商品 / 退款单号</th><th class="num">金额</th><th>原因</th><th>状态</th><th>申请时间</th><th></th>`, rows) : empty("没有退款"))}`;
  $("#rRef").onclick = () => render();
  $$("[data-go]", v).forEach((b) => (b.onclick = () => guard(async () => { await api("POST", `/a/${a.id}/refunds/${b.dataset.go}/open?order_id=${b.dataset.o}`); toast("已在浏览器窗口打开"); }, b)));
  $$("[data-d]", v).forEach((b) => (b.onclick = () => refundDetail(a, b.dataset.d, b.dataset.o)));
}

async function refundDetail(a, rid, oid) {
  const d = await guard(() => api("GET", `/a/${a.id}/refunds/${rid}?order_id=${encodeURIComponent(oid)}`));
  if (!d) return;
  modal(`<h3>${esc(d.status_text)}</h3>
    <dl class="kv"><dt>金额</dt><dd class="price">¥${esc(d.amount)}</dd><dt>原因</dt><dd>${esc(d.reason || "—")}</dd>
      <dt>进度</dt><dd><div class="steps">${d.nodes.map((n) => `<span>${esc(n)}</span>`).join("")}</div></dd></dl>
    ${d.actions.length ? `<h4>操作</h4><div class="row">${d.actions.map((x, i) => `<button data-i="${i}" class="${x.api_name === "mtop.taobao.idle.refund.agree.refund" ? "primary" : ""}">${esc(x.name)}</button>`).join("")}</div>` : ""}
    <h4>客服介入</h4><textarea id="xe" maxlength="500" placeholder="给小二留言"></textarea>
    ${actions(`<button id="xok">提交留言</button>`)}`, (el, close) => {
    $$("[data-i]", el).forEach((b) => (b.onclick = () => guard(async () => {
      const act = d.actions[Number(b.dataset.i)];
      if (act.api_name === "mtop.taobao.idle.refund.agree.refund" || act.kind !== "api") {
        if (act.kind === "url") await api("POST", `/accounts/${a.id}/open?url=${encodeURIComponent(act.url)}`).catch(() => api("POST", `/a/${a.id}/refunds/${rid}/open?order_id=${oid}`));
        else await api("POST", `/a/${a.id}/refunds/${rid}/open?order_id=${oid}`);
        return toast("已在浏览器窗口打开");
      }
      if (!(await confirmBox(`${act.name}？`, act.api_name, true))) return;
      if (taskResult(await api("POST", `/a/${a.id}/refunds/${rid}/action`, act), "已完成")) { close(); render(); }
    }, b)));
    $("#xok", el).onclick = (e) => guard(async () => taskResult(await api("POST", `/a/${a.id}/refunds/${rid}/xiaoer`, { order_id: oid, content: $("#xe", el).value.trim() }), "已提交"), e.target);
  });
}

// ------------------------------------------------------------------ 评价
async function pageRates(v, q) {
  const a = curAcc(); if (!a) { v.innerHTML = ""; return; }
  const type = q.get("type") || "SELLER";
  const d = await api("GET", `/a/${a.id}/rates?rate_type=${type}`);
  const label = { 1: ["好评", "success"], 0: ["中评", ""], "-1": ["差评", "dead"] };
  const rows = d.rates.map((r) => { const [t, cls] = label[r.rate] || [r.rate, ""];
    return `<tr><td class="w0"><span class="pill ${cls}">${esc(t)}</span></td><td>${esc(r.feedback)}${r.tags.length ? `<div class="muted small">${r.tags.map(esc).join(" · ")}</div>` : ""}</td>
    <td class="muted">${esc(r.rater_nick)}</td><td class="muted">${esc(r.created.slice(0, 10))}</td></tr>`; }).join("");
  v.innerHTML = `<div class="toolbar"><div class="seg"><a href="#/rates?type=SELLER" class="${type === "SELLER" ? "on" : ""}">作为卖家</a><a href="#/rates?type=BUYER" class="${type === "BUYER" ? "on" : ""}">作为买家</a></div>
    <span class="spacer"></span><button id="rNew" class="primary">${ic("plus")}写评价</button></div>
    ${card("", d.rates.length ? table(`<th>评分</th><th>内容</th><th>评价人</th><th>时间</th>`, rows) : empty())}`;
  $("#rNew").onclick = () => modal(`<h3>写评价</h3>
    ${field("订单号", `<input id="rt" class="mono">`)}
    <div class="seg" id="rr" style="margin-bottom:14px"><a href="#" data-v="1" class="on">好评</a><a href="#" data-v="0">中评</a><a href="#" data-v="-1">差评</a></div>
    ${field("内容", `<textarea id="rf" maxlength="500"></textarea>`)}
    <label class="check"><input type="checkbox" id="ra">匿名</label>
    ${actions(`<button data-close>取消</button><button class="primary" id="rok">提交</button>`)}`,
    (el, close) => {
      let rate = 1;
      $$("#rr a", el).forEach((x) => (x.onclick = (e) => { e.preventDefault(); rate = Number(x.dataset.v); $$("#rr a", el).forEach((y) => y.classList.toggle("on", y === x)); }));
      $("#rok", el).onclick = (e) => guard(async () => {
        const t = await api("POST", `/a/${a.id}/rates`, { trade_id: $("#rt", el).value.trim(), rate, feedback: $("#rf", el).value.trim(), anonymous: $("#ra", el).checked });
        if (taskResult(t, "已提交")) { close(); render(); }
      }, e.target);
    });
}

// ------------------------------------------------------------------ 任务
async function pageTasks(v, q) {
  const status = q.get("status") || "";
  const [batches, tasks] = await Promise.all([api("GET", "/batches"), api("GET", `/tasks${status ? "?status=" + status : ""}`)]);
  const accName = (id) => (S.accounts.find((a) => a.id === id) || {}).name || `#${id}`;
  const batchRows = batches.map((b) => { const done = b.success + b.dead, pct = b.total ? Math.round((done * 100) / b.total) : 0;
    return `<tr><td class="title-cell">${esc(b.title)}<span class="mono">#${b.id} · ${esc(accName(b.account_id))}</span></td>
      <td><span class="progress"><i style="width:${pct}%"></i></span> <span class="muted small">${done}/${b.total}</span></td>
      <td class="num">${b.dead ? `<span class="pill dead">${b.dead} 失败</span>` : ""}</td><td class="muted">${esc(b.created_at.slice(5, 16).replace("T", " "))}</td></tr>`; }).join("");
  const taskRows = tasks.map((t) => `<tr><td class="title-cell">${esc(t.title)}<span class="mono">#${t.id} · ${esc(accName(t.account_id))}</span></td>
    <td><span class="pill ${esc(t.status)}">${esc(TASK_STATUS[t.status] || t.status)}</span>${t.attempts > 1 ? `<span class="muted small"> ×${t.attempts}</span>` : ""}</td>
    <td class="mono" style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.last_error)}">${esc(t.last_error)}</td>
    <td class="act">${t.status === "dead" ? `<button class="link" data-r="${t.id}">重试</button>` : t.status === "pending" ? `<span class="muted small">${esc(hhmm(t.next_run_at))}</span>` : ""}</td></tr>`).join("");
  const filter = `<div class="seg">${["", "pending", "success", "dead"].map((s) => `<a href="#/tasks${s ? "?status=" + s : ""}" class="${s === status ? "on" : ""}">${s ? TASK_STATUS[s] : "全部"}</a>`).join("")}</div>`;
  v.innerHTML = `${batches.length ? card("批次", table(`<th>批次</th><th>进度</th><th></th><th>创建</th>`, batchRows)) : ""}
    <div class="toolbar">${filter}<span class="spacer"></span><button class="icon" id="tRef" title="刷新">${ic("refresh")}</button></div>
    ${card("", tasks.length ? table(`<th>任务</th><th>状态</th><th>错误</th><th></th>`, taskRows) : empty("暂无任务"))}`;
  $("#tRef").onclick = () => render();
  $$("[data-r]", v).forEach((b) => (b.onclick = () => guard(async () => { await api("POST", `/tasks/${b.dataset.r}/retry`); toast("已重新排队"); render(); }, b)));
}

// ------------------------------------------------------------------ 设置
async function pageSettings(v, q) {
  const tab = q.get("tab") || "params";
  v.innerHTML = `<div class="tabs"><a href="#/settings?tab=params" class="${tab === "params" ? "on" : ""}">运行参数</a><a href="#/settings?tab=rules" class="${tab === "rules" ? "on" : ""}">自动回复</a><a href="#/settings?tab=audit" class="${tab === "audit" ? "on" : ""}">操作日志</a></div><div id="sv">${loading()}</div>`;
  const sv = $("#sv");
  if (tab === "params") {
    const d = await api("GET", "/settings");
    const t = d.tunables;
    const fields = [["qps", "单账号 QPS（≤0.3）", 0.01], ["burst", "突发容量", 1], ["backoff_base", "退避基数（秒）", 0.1], ["backoff_cap", "退避上限（秒）", 1],
      ["max_attempts", "最大重试", 1], ["risk_pause_min", "风控暂停（分钟）", 1], ["login_check_min", "登录检测（分钟）", 1],
      ["autoreply_cooldown_s", "自动回复冷却（秒）", 1], ["autoreply_poll_s", "自动回复轮询（秒）", 1], ["read_cache_s", "读缓存（秒）", 1]];
    sv.innerHTML = card("", `<div class="card-b" style="padding-top:20px;max-width:720px"><div class="settings-form">${fields.map(([k, l, s]) => field(l, `<input type="number" step="${s}" data-k="${k}" value="${t[k]}">`)).join("")}</div>
      <div class="row"><button class="primary" id="sSave">保存</button><span class="muted small">数据连接：真实浏览器</span></div></div>`);
    $("#sSave").onclick = (e) => guard(async () => {
      await api("PUT", "/settings", Object.fromEntries($$("[data-k]", sv).map((i) => [i.dataset.k, Number(i.value)]))); toast("已保存");
    }, e.target);
  } else if (tab === "rules") {
    const rules = await api("GET", "/autoreply/rules");
    const accName = (id) => (id ? (S.accounts.find((a) => a.id === id) || {}).name || `#${id}` : "全部");
    const MT = { contains: "包含", exact: "全等", regex: "正则" };
    const rows = rules.map((r) => `<tr><td class="title-cell">${esc(r.keyword)}</td><td><span class="pill">${esc(MT[r.match_type])}</span></td>
      <td>${esc(r.reply)}</td><td class="muted">${esc(accName(r.account_id))}</td><td class="act"><button class="link danger" data-x="${r.id}">删除</button></td></tr>`).join("");
    sv.innerHTML = `<div class="toolbar"><span class="muted">${rules.length} 条规则</span><span class="spacer"></span><button class="primary" id="rAdd">${ic("plus")}新增规则</button></div>
      ${card("", rules.length ? table(`<th>关键词</th><th>匹配</th><th>回复</th><th>账号</th><th></th>`, rows) : empty("暂无规则"))}`;
    $("#rAdd").onclick = () => modal(`<h3>新增规则</h3>
      <div class="row">${field("关键词", `<input id="rk" maxlength="200">`)}${field("匹配", `<select id="rm"><option value="contains">包含</option><option value="exact">全等</option><option value="regex">正则</option></select>`)}</div>
      ${field("回复", `<textarea id="rp" maxlength="500"></textarea>`)}
      ${field("账号", `<select id="ra"><option value="">全部</option>${S.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select>`)}
      ${actions(`<button data-close>取消</button><button class="primary" id="rok">添加</button>`)}`,
      (el, close) => ($("#rok", el).onclick = (e) => guard(async () => {
        await api("POST", "/autoreply/rules", { account_id: $("#ra", el).value ? Number($("#ra", el).value) : null, keyword: $("#rk", el).value.trim(), match_type: $("#rm", el).value, reply: $("#rp", el).value.trim() });
        close(); render();
      }, e.target)));
    $$("[data-x]", sv).forEach((b) => (b.onclick = () => guard(async () => { await api("DELETE", `/autoreply/rules/${b.dataset.x}`); render(); }, b)));
  } else {
    const rows = await api("GET", "/audit");
    sv.innerHTML = card("", rows.length ? table(`<th>时间</th><th>动作</th><th>内容</th>`,
      rows.map((r) => `<tr><td class="muted w0">${esc(r.created_at.slice(5, 19).replace("T", " "))}</td><td class="mono w0">${esc(r.action)}</td><td>${esc(r.summary)}</td></tr>`).join("")) : empty());
  }
}

// ------------------------------------------------------------------ 路由
const PAGES = { dashboard: pageDashboard, accounts: pageAccounts, items: pageItems, orders: pageOrders, im: pageIm,
  refunds: pageRefunds, rates: pageRates, tasks: pageTasks, settings: pageSettings };

async function render() {
  const [route, qs] = (location.hash.slice(2) || "dashboard").split("?");
  const key = PAGES[route] ? route : "dashboard";
  $$("#nav a").forEach((x) => x.classList.toggle("on", x.dataset.r === key));
  $("#pageTitle").textContent = TITLES[key];
  renderBanner();
  const v = $("#view");
  v.innerHTML = loading();
  try { await PAGES[key](v, new URLSearchParams(qs || "")); }
  catch (e) { v.innerHTML = `<div class="alert bad">${ic("alert")}<span>${esc(e.message)}</span></div>`; }
}

function switchAcc(id) { S.cur = id; lsSet("curAcc", id); $("#accSel").value = id; refreshAccounts(); refreshBadges(); render(); }

async function boot() {
  await refreshAccounts();
  $("#accSel").onchange = (e) => switchAcc(Number(e.target.value));
  window.addEventListener("hashchange", render);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" || e.target.closest(".modal-x")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  render();
  refreshBadges();
  // 后台标签页不轮询：切回来时 window 的 focus 事件会补一次
  S.timer = setInterval(async () => {
    if (document.hidden) return;
    await refreshAccounts().catch(() => {});
    refreshBadges();
  }, 30000);
  window.addEventListener("focus", () => { refreshAccounts().catch(() => {}); refreshBadges(); });
}

boot();
