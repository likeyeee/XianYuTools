"""G2 销售汇总 / G3 账号健康度：全部基于本地数据计算。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..db import CallLog, OrderSnapshot
from .accounts import STATUS_TEXT


class StatsService:
    def __init__(self, session_factory, accounts):
        self.Session = session_factory
        self.accounts = accounts

    def health(self) -> list[dict]:
        since = datetime.now() - timedelta(hours=24)
        out = []
        with self.Session() as s:
            for acc in self.accounts.list():
                base = select(func.count()).select_from(CallLog).where(CallLog.account_id == acc.id, CallLog.created_at >= since)
                total = s.scalar(base) or 0
                ok = s.scalar(base.where(CallLog.ok.is_(True))) or 0
                risk = s.scalar(base.where(CallLog.kind == "risk")) or 0
                last_err = s.scalar(select(CallLog.ret).where(CallLog.account_id == acc.id, CallLog.ok.is_(False))
                                    .order_by(CallLog.id.desc()).limit(1)) or ""
                rt = self.accounts.runtime_if_started(acc.id)
                out.append({"account_id": acc.id, "name": acc.name, "status": acc.status,
                            "status_text": STATUS_TEXT.get(acc.status, acc.status),
                            "resume_at": acc.resume_at.isoformat(timespec="seconds") if acc.resume_at else None,
                            "calls_24h": total, "success_rate": round(ok * 100 / total, 1) if total else None,
                            "risk_24h": risk, "last_error": last_err, "browser": bool(rt)})
        return out

    def record_orders(self, account_id: int, rows: list[tuple[str, float, str]]) -> None:
        """rows: (order_id, amount_yuan, 'YYYY-MM-DD')"""
        with self.Session() as s:
            for oid, amount, day in rows:
                snap = s.get(OrderSnapshot, (account_id, oid))
                if snap is None:
                    s.add(OrderSnapshot(account_id=account_id, order_id=oid, amount=amount, day=day))
            s.commit()

    def sales(self, account_id: int | None = None, days: int = 7) -> list[dict]:
        start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        q = (select(OrderSnapshot.day, func.count(), func.sum(OrderSnapshot.amount))
             .where(OrderSnapshot.day >= start).group_by(OrderSnapshot.day).order_by(OrderSnapshot.day))
        if account_id:
            q = q.where(OrderSnapshot.account_id == account_id)
        with self.Session() as s:
            return [{"day": d, "orders": n, "amount": round(a or 0, 2)} for d, n, a in s.execute(q).all()]
