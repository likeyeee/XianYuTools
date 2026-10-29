"""SQLite 持久化（SQLAlchemy 2.0）。数据最小化：不存买家姓名/手机/地址/聊天正文。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine,
                        event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str] = mapped_column(String(500), default="")
    xy_user_id: Mapped[str] = mapped_column(String(32), default="")
    # new / ok / expired / restricted / disabled
    status: Mapped[str] = mapped_column(String(16), default="new")
    resume_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    autoreply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("idem_key", name="uq_task_idem"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    op: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    title: Mapped[str] = mapped_column(String(128), default="")
    # pending / running / success / dead
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idem_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CallLog(Base):
    __tablename__ = "call_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    api: Mapped[str] = mapped_column(String(128))
    ok: Mapped[bool] = mapped_column(Boolean)
    kind: Mapped[str] = mapped_column(String(16))
    ret: Mapped[str] = mapped_column(Text, default="")
    cost_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AutoReplyRule(Base):
    __tablename__ = "autoreply_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 空 = 全部账号
    keyword: Mapped[str] = mapped_column(String(200))
    match_type: Mapped[str] = mapped_column(String(16), default="contains")  # contains / exact / regex
    reply: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class AutoReplyState(Base):
    __tablename__ = "autoreply_state"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cid: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_msg_id: Mapped[str] = mapped_column(String(64), default="")


class QuickPhrase(Base):
    __tablename__ = "quick_phrases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(String(500))


class OrderSnapshot(Base):
    """销售聚合（G2）只保留金额与日期。"""
    __tablename__ = "order_snapshots"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    day: Mapped[str] = mapped_column(String(10), index=True)


class PeerProfile(Base):
    """聊天对方资料缓存。

    会话列表接口本身不带对方头像，需要按 userId 单独查 user.page.nav；
    每次都查一遍会让消息页慢到不可用，所以缓存下来（头像/昵称变化很慢）。
    """
    __tablename__ = "peer_profiles"
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    peer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    nick: Mapped[str] = mapped_column(String(128), default="")
    avatar: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SettingKV(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


def make_session_factory(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):  # WAL：读写并发更友好
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    Base.metadata.create_all(engine)
    # 轻量迁移：create_all 不会给已有表补列。
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(accounts)")}
        if "avatar" not in cols:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN avatar VARCHAR(500) DEFAULT ''")
    return sessionmaker(engine, expire_on_commit=False)
