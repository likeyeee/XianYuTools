"""D7 自动回复：关键词命中 → 入队发送；同一会话冷却期内不重复回复，同一条消息只回复一次。"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select

from ..db import AutoReplyRule, AutoReplyState

log = logging.getLogger("shopmgr.autoreply")


def match_rule(text: str, rules: list[AutoReplyRule]) -> AutoReplyRule | None:
    """按 sort/id 顺序返回第一条命中的启用规则；非法正则视为不命中。"""
    text = (text or "").strip()
    if not text:
        return None
    for r in sorted(rules, key=lambda x: (x.sort, x.id or 0)):
        if not r.enabled or not r.keyword:
            continue
        if r.match_type == "exact" and text == r.keyword.strip():
            return r
        if r.match_type == "contains" and r.keyword in text:
            return r
        if r.match_type == "regex":
            try:
                if re.search(r.keyword, text):
                    return r
            except re.error:
                continue
    return None


class AutoReplyService:
    def __init__(self, session_factory, accounts, queue, tunables):
        self.Session = session_factory
        self.accounts = accounts
        self.queue = queue
        self.tunables = tunables

    def rules_for(self, account_id: int) -> list[AutoReplyRule]:
        with self.Session() as s:
            q = select(AutoReplyRule).where(or_(AutoReplyRule.account_id.is_(None), AutoReplyRule.account_id == account_id))
            return list(s.scalars(q))

    async def scan_account(self, account_id: int) -> int:
        """扫描一次会话，返回本次入队的回复数。"""
        rules = self.rules_for(account_id)
        if not rules:
            return 0
        rt = self.accounts.runtime(account_id)
        page = await rt.run("lwp:conversations", lambda c: c.conversations(limit=20))
        self_id = rt.client.self_id
        cooldown = timedelta(seconds=self.tunables().autoreply_cooldown_s)
        sent = 0
        for conv in page.conversations:
            # 只处理：有未读，且最后一条来自对方
            if conv.unread <= 0 or not conv.last_sender_id or conv.last_sender_id == self_id:
                continue
            rule = match_rule(conv.last_text, rules)
            if rule is None:
                continue
            msg_key = f"{conv.last_time}"
            with self.Session() as s:
                st = s.get(AutoReplyState, (account_id, conv.cid))
                now = datetime.now()
                if st and (st.last_msg_id == msg_key or (st.last_reply_at and now - st.last_reply_at < cooldown)):
                    continue
                if st is None:
                    st = AutoReplyState(account_id=account_id, cid=conv.cid)
                    s.add(st)
                st.last_reply_at, st.last_msg_id = now, msg_key
                s.commit()
            self.queue.enqueue(account_id, "im.send_text", {"cid": conv.cid, "peer_id": conv.peer_id, "text": rule.reply},
                               title=f"自动回复 → {conv.peer_nick or conv.peer_id}",
                               idem_key=f"autoreply:{account_id}:{conv.cid}:{msg_key}")
            sent += 1
        return sent

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tunables().autoreply_poll_s)
            for acc in self.accounts.list():
                if not acc.autoreply_enabled or acc.status != "ok":
                    continue
                try:
                    await self.scan_account(acc.id)
                except Exception as e:  # noqa: BLE001 - 单账号失败不影响其它账号
                    log.warning("autoreply scan %s failed: %s", acc.id, e)
