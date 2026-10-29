"""聊天对方资料（头像 / 昵称）的缓存与补齐。

会话列表接口本身**不带**对方头像 —— 实测 `userConvs[].singleChatUserConversation` 里
只有 itemId / itemTitle / orderId / extUserId，没有任何 avatar 字段。

查别人用 `mtop.idle.web.user.page.head` + userId（`page.nav` 传 userId 只会返回登录用户
自己，见 D-20）。这个接口**很慢**：实测单个请求就可能 20 秒超时，所以绝不能放在会话
列表接口的同步路径里 —— 那样首屏要等 100 秒以上。这里改成：

1. 会话列表只读缓存，缺失的会话先用昵称首字占位（前端已有能力）；
2. 缺失的 peer 丢给后台任务补，同一 peer 只跑一个任务，失败退避；
3. 前端拿到列表后单独拉一次 `/im/peers` 补头像，局部更新不重建整列。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from ..db import PeerProfile

log = logging.getLogger("shopmgr.peers")

CACHE_DAYS = 30        # 头像/昵称变化很慢，缓存久一点
PREFETCH_LIMIT = 10    # 单次进消息页最多后台补几个（其余下次再补）
FAIL_BACKOFF_S = 600   # 拉失败后 10 分钟内不再重试，避免把慢接口打成限流
PREFETCH_DELAY_S = 1.5  # 后台任务延迟启动，让首屏那次请求先跑完
PREFETCH_GAP_S = 0.6    # 任务之间错开，避免同时挤在页面队列里
FETCH_CONCURRENCY = 2   # 后台同时最多几个在跑


class PeerService:
    def __init__(self, session_factory):
        self.Session = session_factory
        self._tasks: dict[tuple[int, str], asyncio.Task] = {}
        self._fail_at: dict[str, float] = {}
        self._sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    def cached(self, account_id: int, peer_ids: list[str]) -> dict[str, tuple[str, str]]:
        """返回 {peer_id: (nick, avatar)}；过期的当作没缓存。"""
        ids = [p for p in dict.fromkeys(peer_ids) if p]
        if not ids:
            return {}
        cutoff = datetime.now() - timedelta(days=CACHE_DAYS)
        with self.Session() as s:
            rows = s.scalars(select(PeerProfile).where(
                PeerProfile.account_id == account_id,
                PeerProfile.peer_id.in_(ids),
                PeerProfile.updated_at >= cutoff)).all()
            return {r.peer_id: (r.nick, r.avatar) for r in rows}

    def save(self, account_id: int, peer_id: str, nick: str, avatar: str) -> None:
        if not peer_id:
            return
        with self.Session() as s:
            row = s.get(PeerProfile, (account_id, peer_id))
            if row is None:
                row = PeerProfile(account_id=account_id, peer_id=peer_id)
                s.add(row)
            row.nick = (nick or "")[:128]
            row.avatar = (avatar or "")[:500]
            row.updated_at = datetime.now()
            s.commit()

    def attach(self, account_id: int, convs: list) -> list[str]:
        """把缓存里的资料挂到会话上，返回还需要补的 peer_id（不发起任何请求）。"""
        if not convs:
            return []
        cached = self.cached(account_id, [c.peer_id for c in convs])
        todo: list[str] = []
        for c in convs:
            nick, avatar = cached.get(c.peer_id, ("", ""))
            if nick or avatar:
                # 昵称以资料接口为准：client 先用 reminderTitle 猜了一版，
                # 只有"对方发来的最后一条"才准，自己发的会拿到自己的昵称。
                c.peer_nick = nick or c.peer_nick
                c.peer_avatar = avatar or c.peer_avatar
            elif c.peer_id and c.peer_id != "0":
                todo.append(c.peer_id)
        return todo

    def prefetch(self, account_id: int, peer_ids: list[str], client,
                 limit: int = PREFETCH_LIMIT) -> int:
        """后台补取，立即返回。同一个 peer 只跑一个任务；失败过的先退避。

        延迟 + 错开启动：Playwright 的 page.evaluate 在同一个页面上是排队执行的，
        后台补头像如果和前台列表请求同时发，会把首屏刷新拖到 3 秒以上。
        """
        now = time.monotonic()
        started = 0
        for i, pid in enumerate(peer_ids[:limit]):
            key = (account_id, pid)
            running = self._tasks.get(key)
            if running is not None and not running.done():
                continue
            if self._fail_at.get(pid, 0.0) > now:
                continue
            delay = PREFETCH_DELAY_S + i * PREFETCH_GAP_S
            self._tasks[key] = asyncio.create_task(
                self._delayed_fetch(delay, client, account_id, pid))
            started += 1
        return started

    async def _delayed_fetch(self, delay: float, client, account_id: int, peer_id: str) -> None:
        await asyncio.sleep(delay)
        await self._fetch_one(client, account_id, peer_id)

    async def _fetch_one(self, client, account_id: int, peer_id: str) -> None:
        async with self._sem:
            try:
                prof = await client.user_profile(peer_id)
            except Exception as e:  # noqa: BLE001 - 单个用户查不到不影响其它会话
                log.debug("拉取用户资料失败 %s: %s", peer_id, e)
                self._fail_at[peer_id] = time.monotonic() + FAIL_BACKOFF_S
                return
            if prof.nick or prof.avatar:
                self.save(account_id, peer_id, prof.nick, prof.avatar)
                self._fail_at.pop(peer_id, None)

    async def fill(self, account_id: int, convs: list, client, sync_limit: int = 0) -> None:
        """兼容旧调用：只挂缓存，sync_limit>0 时才同步补（测试/脚本用，接口里不要用）。"""
        todo = self.attach(account_id, convs)
        if not todo or sync_limit <= 0:
            if todo:
                self.prefetch(account_id, todo, client)
            return
        await asyncio.gather(*(self._fetch_one(client, account_id, p) for p in todo[:sync_limit]))
        self.attach(account_id, convs)
