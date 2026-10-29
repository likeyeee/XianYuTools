"""账号运行时与账号生命周期。

AccountRuntime：每账号一个，所有平台调用（读和写）都经过它：
    可用性检查 → 令牌桶 → 同账号串行锁 → 调用 → 记录日志 → 按 ret 分类更新账号状态
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import secrets
import shutil
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy import select

from ..config import Settings, Tunables
from ..db import Account, AuditLog, CallLog
from ..scheduler.ratelimit import TokenBucket
from ..xianyu import MtopError, RetKind, XianyuClient
from ..xianyu.transport import Transport

log = logging.getLogger("shopmgr.accounts")
T = TypeVar("T")

STATUS_TEXT = {"new": "未登录", "ok": "正常", "expired": "失效", "restricted": "受限", "disabled": "已停用"}


class AccountUnavailable(Exception):
    def __init__(self, account_id: int, status: str, resume_at: datetime | None = None, message: str = ""):
        self.account_id, self.status, self.resume_at = account_id, status, resume_at
        tip = message or {"restricted": f"账号风控暂停中，预计 {resume_at:%H:%M:%S} 恢复，请在该账号浏览器窗口内手动完成验证"
                          if resume_at else "账号受限", "disabled": "账号已停用"}.get(status, "账号不可用")
        super().__init__(tip)


class AccountRuntime:
    def __init__(self, account: Account, transport: Transport, svc: "AccountService"):
        self.account_id = account.id
        self.transport = transport
        self.client = XianyuClient(transport)
        self.client.self_id = account.xy_user_id or ""
        self.svc = svc
        t = svc.tunables()
        self.bucket = TokenBucket(t.qps, t.burst)
        self.lock = asyncio.Lock()
        self.status = account.status
        self.resume_at = account.resume_at
        self.last_error = account.last_error
        self._cache: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    # ---------------------------------------------------------------- 状态
    def check_available(self) -> None:
        if self.status == "disabled":
            raise AccountUnavailable(self.account_id, "disabled")
        if self.status == "restricted":
            if self.resume_at and self.resume_at > datetime.now():
                raise AccountUnavailable(self.account_id, "restricted", self.resume_at)
            self._set_status("ok", None, "")  # 到期自动恢复

    def _set_status(self, status: str, resume_at: datetime | None, error: str) -> None:
        changed = (status, resume_at) != (self.status, self.resume_at)
        self.status, self.resume_at, self.last_error = status, resume_at, error
        if changed:
            log.info("account %s -> %s %s", self.account_id, status, error)
            self._cache.clear()
            if status == "expired":
                self.svc.note_session_lost(self.account_id, error)
        self.svc.persist_status(self.account_id, status, resume_at, error)

    def apply_kind(self, kind: RetKind, err: str = "") -> None:
        if kind is RetKind.OK:
            if self.status in ("new", "expired"):
                self._set_status("ok", None, "")
        elif kind is RetKind.SESSION:
            self._set_status("expired", None, err)
        elif kind is RetKind.RISK:
            pause = timedelta(minutes=self.svc.tunables().risk_pause_min)
            self._set_status("restricted", datetime.now() + pause, err)
        elif kind is RetKind.BLOCK:
            self._set_status("disabled", None, err)

    def resume(self) -> None:
        if self.status == "restricted":
            self._set_status("ok", None, "")

    # ---------------------------------------------------------------- 调用
    async def run(self, label: str, fn: Callable[[XianyuClient], Awaitable[T]], *, cache_key: str | None = None,
                  affect_status: bool = True) -> T:
        """执行一次平台调用。

        affect_status=False 用于"附带请求"：比如取昵称头像的资料接口，失败只说明这个接口
        不通，不能据此判定整个账号掉登录态（实测 loginuser.get 与 user.page.nav 的
        判定严格度并不一致，前者成功、后者报过期是出现过的）。
        """
        t = self.svc.tunables()
        if cache_key and t.read_cache_s:
            hit = self._cache.get(cache_key)
            if hit and time.monotonic() - hit[0] < t.read_cache_s:
                return hit[1]
            # 同一读请求正在进行时合并等待，不重复占用限速配额
            pending = self._inflight.get(cache_key)
            if pending is not None:
                return await asyncio.shield(pending)
            fut = self._inflight[cache_key] = asyncio.get_running_loop().create_future()
            try:
                res = await self._run(label, fn, t, affect_status)
            except BaseException as e:
                fut.set_exception(e)
                fut.exception()  # 标记已读取，避免无人等待时告警
                raise
            else:
                fut.set_result(res)
                self._cache[cache_key] = (time.monotonic(), res)
                return res
            finally:
                self._inflight.pop(cache_key, None)
        return await self._run(label, fn, t, affect_status)

    async def _run(self, label: str, fn: Callable[[XianyuClient], Awaitable[T]], t,
                   affect_status: bool = True) -> T:
        self.check_available()
        self.bucket.rate, self.bucket.capacity = t.qps, t.burst
        await self.bucket.acquire()
        async with self.lock:
            self.check_available()   # 等锁期间可能已被其它调用置为受限
            if t.mtop_jitter_ms:
                # 请求间隔过于均匀本身就是机器特征，加一点抖动
                await asyncio.sleep(random.uniform(0, t.mtop_jitter_ms / 1000))
            t0 = time.perf_counter()
            try:
                res = await fn(self.client)
            except MtopError as e:
                self.svc.log_call(self.account_id, label, False, e.kind.value, e.ret[0] if e.ret else "", t0)
                if affect_status:
                    self.apply_kind(e.kind, e.ret[0] if e.ret else str(e))
                raise
            self.svc.log_call(self.account_id, label, True, "ok", "", t0)
            if affect_status:
                self.apply_kind(RetKind.OK)
        return res

    def invalidate(self, prefix: str = "") -> None:
        for k in [k for k in self._cache if k.startswith(prefix)]:
            self._cache.pop(k, None)


class AccountService:
    def __init__(self, settings: Settings, session_factory, tunables: Callable[[], Tunables],
                 transport_factory: Callable[[Account], Transport] | None = None):
        self.settings = settings
        self.Session = session_factory
        self.tunables = tunables
        self._factory = transport_factory or self._default_factory
        self._runtimes: dict[int, AccountRuntime] = {}
        self._qr_sessions: dict[int, dict] = {}
        self._device_seed: str | None = None

    def _default_factory(self, acc: Account) -> Transport:
        from ..xianyu.stealth import make_fingerprint
        from ..xianyu.transport_browser import BrowserTransport
        # 指纹种子：默认按"这台机器"派生，所有账号共用一套硬件参数 —— 真实场景就是
        # 一台电脑开多个闲鱼号；按账号派生会让同一 IP 下出现 N 台不同设备，反而更像批量操作。
        seed = acc.id if self.settings.fingerprint_scope == "account" else self.device_seed
        fp = make_fingerprint(seed) if self.settings.spoof_fingerprint else None
        return BrowserTransport(self.settings.profiles_dir / f"acc_{acc.id}",
                                headless=self.settings.headless, fingerprint=fp,
                                session_file=self.settings.sessions_dir / f"acc_{acc.id}.json")

    @property
    def device_seed(self) -> str:
        """本机的设备种子，首次生成后固定，保证浏览器指纹不随账号增删而漂移。"""
        if self._device_seed is None:
            f = self.settings.data_dir / "device.json"
            try:
                self._device_seed = json.loads(f.read_text(encoding="utf-8"))["seed"]
            except Exception:  # noqa: BLE001 - 文件不存在或损坏时重新生成
                self._device_seed = secrets.token_hex(8)
                try:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps({"seed": self._device_seed, "created": int(time.time())}),
                                 encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    log.warning("写入设备种子失败：%s", e)
        return self._device_seed

    def profile_dir(self, account_id: int):
        return self.settings.profiles_dir / f"acc_{account_id}"

    # ---------------------------------------------------------------- 持久化
    def persist_status(self, account_id: int, status: str, resume_at, error: str) -> None:
        with self.Session() as s:
            acc = s.get(Account, account_id)
            if acc:
                acc.status, acc.resume_at, acc.last_error = status, resume_at, error[:500]
                s.commit()

    def log_call(self, account_id: int, api: str, ok: bool, kind: str, ret: str, t0: float) -> None:
        with self.Session() as s:
            s.add(CallLog(account_id=account_id, api=api, ok=ok, kind=kind, ret=ret[:300],
                          cost_ms=int((time.perf_counter() - t0) * 1000)))
            s.commit()

    def audit(self, account_id: int | None, action: str, summary: str) -> None:
        with self.Session() as s:
            s.add(AuditLog(account_id=account_id, action=action, summary=summary[:1000]))
            s.commit()

    def _last_login_at(self, account_id: int) -> datetime | None:
        with self.Session() as s:
            row = s.execute(
                select(AuditLog.created_at)
                .where(AuditLog.account_id == account_id, AuditLog.action == "account.login")
                .order_by(AuditLog.id.desc()).limit(1)
            ).first()
        return row[0] if row else None

    def note_session_lost(self, account_id: int, error: str) -> None:
        """登录态失效时记下"这次登录活了多久"。

        这是判断风控强度的关键指标：正常登录态能撑几天，被风控盯上的通常几分钟到几小时就没了。
        """
        last = self._last_login_at(account_id)
        if last is None:
            return
        alive = (datetime.now() - last).total_seconds()
        if alive < 0:
            return
        if alive < 3600:
            span = f"{int(alive // 60)} 分钟"
        elif alive < 86400:
            span = f"{alive / 3600:.1f} 小时"
        else:
            span = f"{alive / 86400:.1f} 天"
        self.audit(account_id, "account.session_lost",
                   f"登录态失效（存活 {span}）：{(error or '')[:120]}")

    # ---------------------------------------------------------------- 账号
    def list(self) -> list[Account]:
        with self.Session() as s:
            return list(s.scalars(select(Account).order_by(Account.id)))

    def get(self, account_id: int) -> Account | None:
        with self.Session() as s:
            return s.get(Account, account_id)

    def create(self, name: str = "") -> Account:
        with self.Session() as s:
            acc = Account(name=name)
            s.add(acc)
            s.commit()
        self.audit(acc.id, "account.create", name)
        return acc

    def update(self, account_id: int, **fields) -> Account:
        with self.Session() as s:
            acc = s.get(Account, account_id)
            for k, v in fields.items():
                setattr(acc, k, v)
            s.commit()
        rt = self._runtimes.get(account_id)
        if rt and "status" in fields:
            rt.status = fields["status"]
        return acc

    async def delete(self, account_id: int, wipe_profile: bool = False) -> None:
        """删除账号。

        默认保留浏览器 profile 与登录态快照：profile 里装着 cna / sgcookie / IndexedDB 这些
        设备身份，删掉就等于换了一台新设备，同一账号再次登录会被风控当成"异地新设备"，
        典型后果是登录成功但 session 几分钟内失效。只有明确要求彻底清除时才 wipe_profile=True。
        """
        self._qr_sessions.pop(account_id, None)
        await self.stop(account_id)
        with self.Session() as s:
            acc = s.get(Account, account_id)
            if acc:
                s.delete(acc)
                s.commit()
        if wipe_profile:
            shutil.rmtree(self.profile_dir(account_id), ignore_errors=True)
            try:
                (self.settings.sessions_dir / f"acc_{account_id}.json").unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                log.warning("清理登录态快照失败：%s", e)
        self.audit(account_id, "account.delete",
                   f"{account_id}（{'已清除设备' if wipe_profile else '保留设备身份'}）")

    def runtime(self, account_id: int) -> AccountRuntime:
        rt = self._runtimes.get(account_id)
        if rt is None:
            acc = self.get(account_id)
            if acc is None:
                raise KeyError(f"账号 {account_id} 不存在")
            rt = self._runtimes[account_id] = AccountRuntime(acc, self._factory(acc), self)
        return rt

    def runtime_if_started(self, account_id: int) -> AccountRuntime | None:
        return self._runtimes.get(account_id)

    async def start(self, account_id: int) -> AccountRuntime:
        rt = self.runtime(account_id)
        await rt.transport.start()
        return rt

    async def stop(self, account_id: int) -> None:
        self._qr_sessions.pop(account_id, None)
        rt = self._runtimes.pop(account_id, None)
        if rt:
            await rt.transport.close()

    async def close_all(self) -> None:
        self._qr_sessions.clear()
        for aid in list(self._runtimes):
            await self.stop(aid)

    async def check(self, account_id: int) -> dict:
        """A2 登录态检测（绕过缓存，限速照常）。"""
        rt = self.runtime(account_id)
        try:
            await rt.transport.start()
            user = await rt.run("mtop.taobao.idlemessage.pc.loginuser.get", lambda c: c.login_user())
        except MtopError as e:
            self.update(account_id, last_check_at=datetime.now())
            return {"ok": False, "status": rt.status, "error": e.ret[0] if e.ret else str(e)}
        except AccountUnavailable as e:
            return {"ok": False, "status": e.status, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - 浏览器启动失败等
            self.update(account_id, last_check_at=datetime.now(), last_error=str(e)[:500])
            return {"ok": False, "status": rt.status, "error": str(e)}
        self.update(account_id, xy_user_id=user.user_id, last_check_at=datetime.now())
        rt.client.self_id = user.user_id
        profile_ok = bool(user.nick)
        try:
            profile = await rt.run("mtop.idle.web.user.page.nav", lambda c: c.user_profile(),
                                   affect_status=False)   # 资料接口失败不代表掉登录态
        except Exception as e:  # noqa: BLE001 - 登录态正常时资料接口偶发失败不改变账号状态
            log.warning("profile sync failed for account %s: %s", account_id, e)
        else:
            profile_ok = bool(profile.nick)
            self.update(account_id, name=profile.nick, avatar=profile.avatar)
        if not profile_ok and user.nick:
            self.update(account_id, name=user.nick)
        return {"ok": True, "status": rt.status, "user_id": user.user_id, "profile_ok": profile_ok}

    @staticmethod
    def _qr_data_url(content: str) -> str:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_Q)
        buf = io.BytesIO()
        img.save(buf)
        return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()

    async def start_qr_login(self, account_id: int) -> dict:
        """A1 应用内扫码：在隐藏上下文里取二维码，不打开任何可见窗口。"""
        rt = self.runtime(account_id)          # 只取运行时，不启动账号窗口
        starter = getattr(rt.transport, "start_qr_login", None)
        if starter is None:
            raise ValueError("当前传输不支持应用内二维码登录")
        # 冷却：同一账号短时间内反复扫码会被判成异常登录，服务端会下发短命 session
        wait = self._login_cooldown_left(account_id)
        if wait > 0:
            raise ValueError(f"该账号 {int(wait)} 秒前刚登录过，请 {int(wait)} 秒后再试"
                             "（短时间反复扫码会触发风控，登录态反而更短命）")
        try:
            data = await starter()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"二维码生成失败：{e}") from e
        token = secrets.token_hex(8)
        self._qr_sessions[account_id] = {
            "token": token, "t": data["t"], "ck": data["ck"],
            "created": time.monotonic(), "confirmed": False, "last_status": "NEW",
        }
        self.audit(account_id, "account.qr_login", "生成应用内扫码二维码")
        return {"token": token, "account_id": account_id,
                "qr_data_url": self._qr_data_url(data["code_content"]), "expires_in": 300}

    def _login_cooldown_left(self, account_id: int) -> float:
        """距离上次成功登录还差多少秒才允许再次扫码（0 表示可以）。"""
        cooldown = self.tunables().qr_login_cooldown_s
        if cooldown <= 0:
            return 0.0
        last = self._last_login_at(account_id)
        if last is None:
            return 0.0
        return max(0.0, cooldown - (datetime.now() - last).total_seconds())

    async def poll_qr_login(self, account_id: int, token: str) -> dict:
        """轮询扫码状态；确认后同步真实昵称和头像，资料未就绪时继续重试。"""
        session = self._qr_sessions.get(account_id)
        if session is None or session["token"] != token:
            raise ValueError("二维码会话已失效，请刷新二维码")
        if not session["confirmed"] and time.monotonic() - session["created"] > 300:
            self._qr_sessions.pop(account_id, None)
            return {"status": "EXPIRED", "message": "二维码已过期"}
        if session["confirmed"]:
            return await self._finish_qr_login(account_id)
        rt = self.runtime(account_id)
        poller = getattr(rt.transport, "poll_qr_login", None)
        if poller is None:
            raise ValueError("当前传输不支持应用内二维码登录")
        try:
            data = await poller(session["t"], session["ck"])
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"二维码状态读取失败：{e}") from e
        status = str(data.get("status") or "UNKNOWN")
        session["last_status"] = status
        if status == "CONFIRMED":
            session["confirmed"] = True
            return await self._finish_qr_login(account_id)
        messages = {"NEW": "等待扫码", "SCANED": "已扫码，请在手机上确认",
                    "SCANNED": "已扫码，请在手机上确认", "EXPIRED": "二维码已过期",
                    "CANCELED": "已在手机上取消登录", "ERROR": data.get("title") or "登录失败"}
        return {"status": status, "message": messages.get(status, "等待登录结果")}

    async def _finish_qr_login(self, account_id: int) -> dict:
        rt = self.runtime_if_started(account_id)
        # 先关掉隐藏上下文（登录 Cookie 交给账号窗口），再启动可见窗口读取资料
        closer = getattr(rt.transport, "close_login_page", None) if rt is not None else None
        if closer is not None:
            await closer()
        reload_home = getattr(rt.transport, "reload_home", None) if rt is not None else None
        if reload_home is not None:
            await reload_home()      # 让已打开的首页拿到刚写入的登录 Cookie
        res = await self.check(account_id)
        if not res.get("ok"):
            return {"status": "CONFIRMED", "message": "登录已确认，正在建立登录态",
                    "error": res.get("error") or ""}
        acc = self.get(account_id)
        if acc is None or not acc.name:
            return {"status": "CONFIRMED", "message": "登录成功，正在读取闲鱼昵称与头像"}
        self._qr_sessions.pop(account_id, None)
        self.audit(account_id, "account.login", f"扫码登录成功：{acc.name}")
        return {"status": "CONFIRMED", "message": "登录成功"}

    async def cancel_qr_login(self, account_id: int) -> None:
        """取消扫码：删掉还没登录成功的临时账号，但保留 profile。

        profile 里是设备身份（cna / sgcookie / IndexedDB），反复"添加账号→取消"如果每次都清掉，
        等于每次换一台新设备，下次扫码更容易被判异常登录。
        """
        self._qr_sessions.pop(account_id, None)
        acc = self.get(account_id)
        await self.stop(account_id)
        if acc is not None and not acc.name:
            await self.delete(account_id, wipe_profile=False)

    async def health_loop(self) -> None:
        """定时登录态巡检：只检查已启动过浏览器（有运行时）的账号，避免无意义地拉起窗口。"""
        while True:
            await asyncio.sleep(self.tunables().login_check_min * 60)
            for acc in self.list():
                if acc.status == "disabled" or acc.id not in self._runtimes:
                    continue
                try:
                    await self.check(acc.id)
                except Exception:  # noqa: BLE001
                    log.exception("health check failed: %s", acc.id)
