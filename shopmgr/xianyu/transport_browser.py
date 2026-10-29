"""真实浏览器传输：每个账号一个持久化 profile 的无头 Chrome（需要人工操作时才切出可见窗口）。

请求全部由页面自己的客户端发出（window.lib.mtop.request / 页面已建立的 IM 长连接 /
页面内 fetch 上传），登录态与请求头由页面环境提供。

指纹伪装见 stealth.py：按账号派生稳定的 UA / 屏幕 / 硬件参数，关掉 `--enable-automation`，
补齐无头环境的失真特征。不处理验证码：命中风控时由上层暂停账号，并提示使用者在
「窗口」里手动完成验证。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from pathlib import Path

from .stealth import JS_STEALTH, viewport_of

log = logging.getLogger("shopmgr.transport")

HOME_URL = "https://www.goofish.com/"
IM_URL = "https://www.goofish.com/im"
UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=fleamarket&_input_charset=utf-8"
WS_PATTERN = r"dingtalk\.com|dingtalk\.cn"   # 只收录 IM 长连接（JS 正则源码）
LOGIN_URL = ("https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web"
             "&styleType=vertical&bizParams=&notLoadSsoView=false&notKeepLogin=false&isMobile=false"
             "&qrCodeFirst=true&stie=77")

# 扫码登录：二维码由 passport 页面的同源接口生成，前端只接收 codeContent 后自行渲染。
JS_QR_GENERATE = """
async () => {
    const v = window.viewData || {};
    const p = new URLSearchParams({
        appName: v.appName || "xianyu",
        fromSite: "77",
        appEntrance: v.appEntrance || "web",
        _csrf_token: v._csrf_token || "",
        umidToken: v.umidToken || "",
        hsiz: "",
        bizParams: "taobaoBizLoginFrom=web&renderRefer=https%3A%2F%2Fwww.goofish.com%2F",
        mainPage: "false", isMobile: "false", lang: "zh_CN", umidTag: "SERVER"
    });
    try {
        const r = await fetch("/newlogin/qrcode/generate.do?" + p.toString(), {credentials: "include"});
        const j = await r.json();
        const d = j && j.content && j.content.data;
        if (!d || !d.codeContent) return {ok: false, err: (j && j.content && j.content.errorMsg) || "二维码生成失败"};
        return {ok: true, data: {t: d.t, ck: d.ck, code_content: d.codeContent}};
    } catch (e) {
        return {ok: false, err: String(e)};
    }
}
"""

JS_QR_CHECK = """
async ([t, ck]) => {
    const v = window.viewData || {};
    const body = new URLSearchParams({
        t: String(t), ck: String(ck), ua: "", appName: "xianyu", appEntrance: "web",
        _csrf_token: v._csrf_token || "", umidToken: v.umidToken || "", hsiz: String(ck),
        bizParams: "taobaoBizLoginFrom=web&renderRefer=https%3A%2F%2Fwww.goofish.com%2F",
        mainPage: "false", isMobile: "false", lang: "zh_CN", returnUrl: "", fromSite: "77",
        umidTag: "SERVER", navlanguage: navigator.language, navUserAgent: navigator.userAgent,
        navPlatform: navigator.platform, isIframe: "true", documentReferer: "https://www.goofish.com/",
        defaultView: "sms", deviceId: "", pageTraceId: ""
    });
    try {
        const r = await fetch("/newlogin/qrcode/query.do?appName=xianyu&fromSite=77", {
            method: "POST", credentials: "include",
            headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: body.toString()
        });
        const j = await r.json();
        const d = j && j.content && j.content.data;
        if (!d) return {ok: false, err: (j && j.content && j.content.errorMsg) || "二维码状态读取失败"};
        return {ok: true, data: {status: d.qrCodeStatus || "UNKNOWN", result_code: d.resultCode,
                                  title: d.titleMsg || ""}};
    } catch (e) {
        return {ok: false, err: String(e)};
    }
}
"""

# 调用页面内 mtop 客户端；reject 时把 ret 带回来，交给上层分类
JS_MTOP_CALL = """
async ([api, ver, data]) => {
    const m = (window.lib && window.lib.mtop) || window.mtop;
    if (!m || typeof m.request !== "function") return {ok: false, err: "lib.mtop 未加载"};
    try {
        const res = await m.request({api: api, v: ver, type: "POST", dataType: "json", data: data});
        return {ok: true, res: res};
    } catch (e) {
        const ret = e && (e.ret || (e.errorExtra && e.errorExtra.ret));
        return {ok: false, ret: ret || null, err: String((e && (e.message || e.ret)) || e)};
    }
}
"""

# 记录页面自身创建的 IM WebSocket，并按 headers.mid 匹配响应帧
JS_WS_HOOK = """
(() => {
    if (window.__xy) return;
    const xy = window.__xy = {socks: [], pending: {}};
    xy.adopt = function (ws) {
        if (xy.socks.indexOf(ws) >= 0) return;
        xy.socks.push(ws);
        ws.addEventListener("message", function (ev) {
            if (typeof ev.data !== "string") return;
            let f; try { f = JSON.parse(ev.data); } catch (e) { return; }
            const mid = f && f.headers && f.headers.mid;
            if (mid && xy.pending[mid]) { const cb = xy.pending[mid]; delete xy.pending[mid]; cb(f); }
        });
        ws.addEventListener("close", function () {
            const i = xy.socks.indexOf(ws); if (i >= 0) xy.socks.splice(i, 1);
        });
    };
    const Native = window.WebSocket;
    if (!Native) return;
    const Wrapped = function (url, protocols) {
        const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
        try { if (new RegExp(__WS_PATTERN__).test(String(url))) xy.adopt(ws); } catch (e) {}
        return ws;
    };
    Wrapped.prototype = Native.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) { Wrapped[k] = Native[k]; });
    window.WebSocket = Wrapped;
})();
"""

JS_LWP_CALL = """
async ([route, body, timeout]) => {
    const xy = window.__xy;
    const ws = xy && xy.socks.find(function (s) { return s.readyState === 1; });
    if (!ws) return {ok: false, err: "IM 长连接未就绪"};
    const mid = "" + Math.floor(1000 * Math.random()) + Date.now() + " 0";
    const wait = new Promise(function (resolve) {
        xy.pending[mid] = resolve;
        setTimeout(function () { if (xy.pending[mid]) { delete xy.pending[mid]; resolve(null); } }, timeout);
    });
    ws.send(JSON.stringify({lwp: route, headers: {mid: mid}, body: body}));
    const f = await wait;
    return f ? {ok: true, res: f} : {ok: false, err: "LWP_TIMEOUT"};
}
"""

JS_UPLOAD = """
async ([url, name, b64, mime]) => {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const fd = new FormData();
    fd.append("file", new Blob([arr], {type: mime}), name);
    try {
        const r = await fetch(url, {method: "POST", body: fd, credentials: "include"});
        return await r.json();
    } catch (e) {
        return {success: false, errorCode: "BRIDGE_ERROR", message: String(e)};
    }
}
"""


class BrowserTransport:
    """一个账号 = 一个 persistent context（独立 user-data-dir）。"""

    def __init__(self, profile_dir: Path, *, headless: bool = False, lwp_timeout_ms: int = 15000,
                 home_url: str = HOME_URL, im_url: str = IM_URL, upload_url: str = UPLOAD_URL,
                 ws_pattern: str = WS_PATTERN, fingerprint: dict | None = None,
                 session_file: Path | None = None):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._headless_default = headless   # 人工验证结束后回到这个模式
        self.lwp_timeout_ms = lwp_timeout_ms
        self.home_url, self.im_url, self.upload_url = home_url, im_url, upload_url
        self.ws_hook = JS_WS_HOOK.replace("__WS_PATTERN__", json.dumps(ws_pattern))
        # 指纹伪装（见 stealth.py）：为 None 时不打补丁，保持原样
        self.fp = fingerprint
        self.stealth_js = (JS_STEALTH.replace("__FP__", json.dumps(fingerprint, ensure_ascii=False))
                           if fingerprint else None)
        # 登录态快照：Chrome 把 cookie2 / _tb_token_ 这类 session cookie 存在内存里，
        # 上下文一重启就丢（open_page 切窗口、服务重启都会触发）。这里落一份到磁盘兜底。
        self.session_file = Path(session_file) if session_file else None
        self._pw = None
        self._ctx = None
        self._home = None
        self._im = None
        self._qr_ctx = None          # 隐藏上下文：只用来取二维码，不产生可见窗口
        self._qr_page = None
        self._login_cookies: list = []   # 扫码确认后从隐藏上下文带出的 Cookie
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def _ensure_pw(self):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        return self._pw

    # ---------------------------------------------------------------- 登录态快照
    # 闲鱼登录态的核心是 cookie2 + _tb_token_ + unb + sgcookie，其中
    # cookie2 / _tb_token_ / _samesite_flag_ 是 session cookie（expires = -1），
    # 只活在浏览器进程内存里 —— 上下文一重启（切窗口、服务重启、崩溃）就没了，
    # 服务端随即返回 FAIL_SYS_SESSION_EXPIRED。所以必须自己落一份到磁盘。
    SESSION_TTL_S = 30 * 86400          # 落盘后给 session cookie 补的有效期
    COOKIE_DOMAINS = ("goofish.com", "taobao.com", "mmstat.com", "aliapp.org", "alicdn.com")

    def _keep_cookie(self, c: dict) -> bool:
        dom = str(c.get("domain") or "").lstrip(".")
        return any(dom == d or dom.endswith("." + d) for d in self.COOKIE_DOMAINS)

    def _dump_cookie(self, c: dict) -> dict | None:
        """转成可落盘结构；session cookie 补一个远期 expires，值本身不动。

        已过期的 cookie 返回 None（直接丢弃）—— 补有效期只适用于 session cookie，
        给过期 cookie 续命等于把死凭证当活凭证用。
        """
        out = {k: c.get(k) for k in ("name", "value", "domain", "path")}
        out["path"] = out["path"] or "/"
        out["httpOnly"] = bool(c.get("httpOnly"))
        out["secure"] = bool(c.get("secure"))
        same = c.get("sameSite")
        if same in ("Strict", "Lax", "None"):
            out["sameSite"] = same
        exp = c.get("expires", -1)
        if exp and exp > 0:
            if exp < time.time():
                return None
            out["expires"] = exp
        else:
            out["expires"] = time.time() + self.SESSION_TTL_S
        return out

    def save_cookies(self, cookies) -> int:
        """把上下文里的 Cookie 落盘。返回保存条数。"""
        if not self.session_file or not cookies:
            return 0
        rows = []
        for c in cookies:
            if not self._keep_cookie(c) or not c.get("name"):
                continue
            row = self._dump_cookie(c)
            if row is not None:
                rows.append(row)
        if not rows:
            return 0
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.session_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved_at": int(time.time()), "cookies": rows},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.session_file)      # 原子写，避免半截文件
        except Exception as e:  # noqa: BLE001 - 快照失败不能影响主流程
            log.warning("保存登录态快照失败 %s: %s", self.session_file, e)
            return 0
        return len(rows)

    def load_cookies(self) -> list:
        """读取磁盘上的登录态快照；已过期的丢掉。"""
        if not self.session_file or not self.session_file.exists():
            return []
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("读取登录态快照失败 %s: %s", self.session_file, e)
            return []
        now = time.time()
        out = []
        for c in data.get("cookies") or []:
            if not c.get("name") or not c.get("domain"):
                continue
            exp = c.get("expires", -1)
            if exp and 0 < exp < now:
                continue
            out.append({k: v for k, v in c.items() if v is not None})
        return out

    async def _snapshot(self, ctx) -> None:
        """从上下文取一次 Cookie 并落盘（上下文失效时静默跳过）。"""
        if ctx is None or not self.session_file:
            return
        try:
            self.save_cookies(await ctx.cookies())
        except Exception as e:  # noqa: BLE001
            log.debug("取 Cookie 失败，跳过快照: %s", e)

    def _launch_kwargs(self, headless: bool | None = None) -> dict:
        """统一的启动参数：关掉自动化标识，按指纹给出 UA / 视口 / 屏幕。"""
        hl = self.headless if headless is None else headless
        kw = {
            "channel": "chrome",
            "headless": hl,
            # Playwright 默认带 --enable-automation，会让 navigator.webdriver 为 true
            "ignore_default_args": ["--enable-automation"],
            "args": ["--no-first-run", "--no-default-browser-check",
                     "--disable-blink-features=AutomationControlled"],
        }
        if self.fp:
            kw["user_agent"] = self.fp["ua"]
            kw["locale"] = "zh-CN"
            if hl:
                kw["viewport"] = viewport_of(self.fp)
                kw["device_scale_factor"] = self.fp["dpr"]
            else:
                kw["viewport"] = None
                kw["args"].append(f"--window-size={self.fp['screen'][0]},{self.fp['screen'][1]}")
        else:
            kw["viewport"] = None
            if not hl:
                kw["args"].append("--window-size=1280,860")
        return kw

    async def _apply_init_scripts(self, ctx) -> None:
        if self.stealth_js:
            await ctx.add_init_script(self.stealth_js)   # 必须先于 ws_hook，页面脚本之前生效
        await ctx.add_init_script(self.ws_hook)

    async def start(self) -> None:
        async with self._lock:
            if self._ctx is not None:
                return
            pw = await self._ensure_pw()

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._ctx = await pw.chromium.launch_persistent_context(
                str(self.profile_dir), **self._launch_kwargs())
            await self._apply_init_scripts(self._ctx)
            self._ctx.on("close", lambda *_: self._reset())
            if self._login_cookies:
                # 扫码确认后隐藏上下文里拿到的 Cookie 直接注入，避免依赖磁盘落盘时机
                try:
                    await self._ctx.add_cookies(self._login_cookies)
                finally:
                    self._login_cookies = []
            else:
                # 没有待注入的 Cookie 就从磁盘快照恢复：session cookie 不落盘的话，
                # 每次上下文重启都会掉登录态（切窗口 / 服务重启 / 崩溃恢复都会触发）
                saved = self.load_cookies()
                if saved:
                    try:
                        await self._ctx.add_cookies(saved)
                        log.info("从快照恢复登录态：%d 条 cookie（%s）", len(saved), self.session_file)
                    except Exception as e:  # noqa: BLE001 - 快照损坏时退回 profile 自带 Cookie
                        log.warning("注入登录态快照失败：%s", e)
            self._home = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._ensure_home()

    def _reset(self) -> None:
        self._ctx = self._home = self._im = None
        # 窗口被关闭（用户手动关掉，或人工验证结束）后恢复默认模式，避免下次又弹出窗口
        self.headless = self._headless_default

    async def close(self) -> None:
        await self._close_qr_context()
        self._login_cookies = []      # 关账号窗口即丢弃待注入的 Cookie
        async with self._lock:
            ctx, pw = self._ctx, self._pw
            await self._snapshot(ctx)   # 关之前先落盘，否则 cookie2 这类 session cookie 直接丢
            self._reset()
            self._pw = None
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001 - 用户可能已手动关闭窗口
                    pass
            if pw is not None:
                await pw.stop()

    async def _wait_mtop(self, page) -> None:
        for _ in range(60):
            if await page.evaluate("() => !!(window.lib && window.lib.mtop)"):
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("页面内 lib.mtop 未就绪")

    async def _ensure_home(self):
        if self._ctx is None:
            await self.start()
        page = self._home
        if page is None or page.is_closed():
            page = self._home = await self._ctx.new_page()
        if not page.url.startswith(self.home_url):
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=90000)
        await self._wait_mtop(page)
        return page

    async def _open_qr_context(self):
        """扫码用的隐藏上下文：headless 打开 passport 页，全程不产生任何可见窗口。

        与账号窗口共用同一个 profile 目录，所以必须保证同一时刻只有一个上下文在跑；
        账号窗口已开时先关掉它（重新登录场景下旧登录态本来也已失效）。
        """
        if self._qr_page is not None and not self._qr_page.is_closed():
            return self._qr_page
        if self._qr_ctx is not None:      # 上下文还在、页面被关掉了：重开一页即可
            page = self._qr_page = await self._qr_ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_function("() => !!(window.viewData && window.viewData.umidToken)",
                                         timeout=30000)
            return page
        if self._ctx is not None:
            await self.close()
        pw = await self._ensure_pw()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._qr_ctx = await pw.chromium.launch_persistent_context(
            str(self.profile_dir), **self._launch_kwargs(headless=True))
        await self._apply_init_scripts(self._qr_ctx)
        page = self._qr_ctx.pages[0] if self._qr_ctx.pages else await self._qr_ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_function("() => !!(window.viewData && window.viewData.umidToken)", timeout=30000)
        self._qr_page = page
        return page

    async def _close_qr_context(self) -> None:
        """关掉隐藏上下文：先取出 Cookie 供账号窗口注入，再落盘关闭。"""
        ctx, self._qr_ctx, self._qr_page = self._qr_ctx, None, None
        if ctx is None:
            return
        try:
            cookies = await ctx.cookies()
            if cookies:
                self._login_cookies = cookies
                self.save_cookies(cookies)      # 同时落盘，内存丢了也能从磁盘恢复
        except Exception:  # noqa: BLE001 - 取 Cookie 失败时退化为依赖磁盘落盘
            pass
        try:
            await ctx.close()
        except Exception:  # noqa: BLE001
            pass

    async def _set_window_state(self, page, state: str) -> None:
        try:
            cdp = await self._ctx.new_cdp_session(page)
            info = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {
                "windowId": info["windowId"], "bounds": {"windowState": state}})
            await cdp.detach()
        except Exception:  # noqa: BLE001 - 窗口管理失败不影响接口调用
            pass

    async def start_qr_login(self) -> dict:
        page = await self._open_qr_context()
        out = await page.evaluate(JS_QR_GENERATE)
        if not out.get("ok"):
            raise RuntimeError(out.get("err") or "二维码生成失败")
        return out["data"]

    async def poll_qr_login(self, t: int, ck: str) -> dict:
        page = await self._open_qr_context()
        out = await page.evaluate(JS_QR_CHECK, [t, ck])
        if not out.get("ok"):
            raise RuntimeError(out.get("err") or "二维码状态读取失败")
        return out["data"]

    async def close_login_page(self) -> None:
        """扫码确认后调用：关闭隐藏上下文，把登录 Cookie 留给账号窗口。

        关之前先在隐藏上下文里过一遍首页 —— 扫码时页面里的 `_m_h5_tk` 还是**登录前**
        的游客 token，直接导出会把游客 token 和新的 cookie2 一起交给账号窗口，
        服务端看到"新 session 配旧 token"容易判成异常。过一遍首页让页面自己换一次 token。
        """
        page = self._qr_page
        if page is not None and not page.is_closed():
            try:
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=60000)
                await self._wait_mtop(page)
                await page.wait_for_timeout(1500)      # 留出一次 mtop token 换取的时间
            except Exception as e:  # noqa: BLE001 - 换 token 失败不阻断登录，只是少一层保险
                log.debug("登录后预热首页失败：%s", e)
        await self._close_qr_context()

    async def reload_home(self) -> None:
        """扫码确认后刷新首页，让页面重新带上新的登录态 Cookie（否则旧页面仍是登出状态）。"""
        page = self._home
        if page is None or page.is_closed():
            return
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001 - 刷新失败时交给 _ensure_home 重新导航
            self._home = None

    async def _ensure_im(self):
        if self._ctx is None:
            await self.start()
        page = self._im
        if page is None or page.is_closed():
            page = self._im = await self._ctx.new_page()
            await page.goto(self.im_url, wait_until="domcontentloaded", timeout=90000)
        for _ in range(40):
            if await page.evaluate("() => !!(window.__xy && window.__xy.socks.some(s => s.readyState === 1))"):
                return page
            await asyncio.sleep(0.5)
        raise RuntimeError("IM 长连接未建立，请确认该账号已登录且 IM 页面可正常打开")

    async def mtop(self, api: str, v: str, data: dict) -> dict:
        try:
            page = await self._ensure_home()
            out = await page.evaluate(JS_MTOP_CALL, [api, v, data])
        except Exception as exc:  # noqa: BLE001 - 浏览器被关闭、导航中断等
            return {"api": api, "v": v, "ret": [f"BRIDGE_ERROR::{exc}"], "data": {}}
        if out.get("ok"):
            return out["res"]
        ret = out.get("ret") or [f"BRIDGE_ERROR::{out.get('err')}"]
        return {"api": api, "v": v, "ret": ret, "data": {}}

    async def lwp(self, route: str, body: list) -> dict:
        try:
            page = await self._ensure_im()
            out = await page.evaluate(JS_LWP_CALL, [route, body, self.lwp_timeout_ms])
        except Exception as exc:  # noqa: BLE001
            return {"code": 0, "err": f"BRIDGE_ERROR::{exc}", "body": {}}
        if out.get("ok"):
            return out["res"]
        return {"code": 0, "err": out.get("err"), "body": {}}

    async def upload(self, filename: str, content: bytes) -> dict:
        page = await self._ensure_home()
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        b64 = base64.b64encode(content).decode()
        return await page.evaluate(JS_UPLOAD, [self.upload_url, filename, b64, mime])

    async def open_page(self, url: str) -> None:
        """把官方页面显示出来给本人操作（滑块、支付密码等）。

        平时浏览器是无头的（不显示任何窗口）；这里如果还处于无头状态，就换成可见窗口重启一次，
        登录态在同一个 profile 里，重启后仍然是已登录。
        """
        if self.headless:
            await self.close()
            self.headless = False
        if self._ctx is None:
            await self.start()
        page = await self._ctx.new_page()
        await self._set_window_state(page, "normal")
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.bring_to_front()
