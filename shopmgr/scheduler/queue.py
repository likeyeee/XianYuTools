"""持久化任务队列：同账号串行、跨账号并行；失败指数退避；风控暂停；死信可人工重试。

状态机见架构设计 2.4：
    pending → running → success
                     ↘ pending（可重试 / 账号暂停）
                     ↘ dead（超过重试上限 / 业务错误 / 登录失效 / 封禁）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select, update

from ..config import Tunables
from ..db import Batch, Task
from ..xianyu import MtopError, RetKind
from .backoff import backoff_delay
from .ops import OPS, to_jsonable

log = logging.getLogger("shopmgr.queue")


class TaskQueue:
    def __init__(self, session_factory, accounts, tunables: Callable[[], Tunables], poll_interval: float = 0.5):
        self.Session = session_factory
        self.accounts = accounts            # AccountService
        self.tunables = tunables
        self.poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._busy: set[int] = set()
        self._done: dict[int, asyncio.Event] = {}
        self._running: set[asyncio.Task] = set()
        self._stopped = False

    # ---------------------------------------------------------------- 入队
    def enqueue(self, account_id: int, op: str, params: dict, *, title: str = "", batch_id: int | None = None,
                idem_key: str | None = None) -> Task:
        if op not in OPS:
            raise ValueError(f"未注册的操作: {op}")
        with self.Session() as s:
            if idem_key:
                exist = s.scalar(select(Task).where(Task.idem_key == idem_key))
                if exist and exist.status == "dead":
                    # 同一业务动作再次提交 = 人工重试；成功/进行中的则直接返回，避免重复发货等
                    s.commit()
                    return self.retry(exist.id)
                if exist:
                    return exist
            task = Task(account_id=account_id, op=op, params=params, title=title or op, batch_id=batch_id,
                        idem_key=idem_key, max_attempts=self.tunables().max_attempts, next_run_at=datetime.now())
            s.add(task)
            s.commit()
        self.accounts.audit(account_id, f"enqueue:{op}", f"task={task.id} {title}")
        self._wake.set()
        return task

    def create_batch(self, account_id: int, kind: str, title: str, jobs: list[tuple[str, dict, str]]) -> Batch:
        with self.Session() as s:
            batch = Batch(account_id=account_id, kind=kind, title=title, total=len(jobs))
            s.add(batch)
            s.commit()
        for op, params, t in jobs:
            self.enqueue(account_id, op, params, title=t, batch_id=batch.id)
        return batch

    def retry(self, task_id: int) -> Task:
        with self.Session() as s:
            task = s.get(Task, task_id)
            if task is None or task.status != "dead":
                raise ValueError("只有死信任务可以重试")
            task.status, task.attempts, task.next_run_at, task.last_error = "pending", 0, datetime.now(), ""
            s.commit()
        self._wake.set()
        return task

    def recover(self) -> int:
        """进程重启：running 视为中断，回到 pending。"""
        with self.Session() as s:
            n = s.execute(update(Task).where(Task.status == "running").values(status="pending")).rowcount
            s.commit()
        return n or 0

    # ---------------------------------------------------------------- 查询
    def get(self, task_id: int) -> Task | None:
        with self.Session() as s:
            return s.get(Task, task_id)

    def list(self, *, status: str | None = None, account_id: int | None = None, batch_id: int | None = None,
             limit: int = 200) -> list[Task]:
        q = select(Task).order_by(Task.id.desc()).limit(limit)
        if status:
            q = q.where(Task.status == status)
        if account_id:
            q = q.where(Task.account_id == account_id)
        if batch_id:
            q = q.where(Task.batch_id == batch_id)
        with self.Session() as s:
            return list(s.scalars(q))

    def batches(self, limit: int = 50) -> list[dict]:
        with self.Session() as s:
            rows = list(s.scalars(select(Batch).order_by(Batch.id.desc()).limit(limit)))
            out = []
            for b in rows:
                counts = dict(s.execute(select(Task.status, func.count()).where(Task.batch_id == b.id)
                                        .group_by(Task.status)).all())
                out.append({"id": b.id, "kind": b.kind, "title": b.title, "account_id": b.account_id, "total": b.total,
                            "success": counts.get("success", 0), "dead": counts.get("dead", 0),
                            "pending": counts.get("pending", 0) + counts.get("running", 0),
                            "created_at": b.created_at.isoformat(timespec="seconds")})
            return out

    def counts(self) -> dict[str, int]:
        with self.Session() as s:
            return dict(s.execute(select(Task.status, func.count()).group_by(Task.status)).all())

    async def wait(self, task_id: int, timeout: float = 30) -> Task:
        """等待任务到达终态（success / dead）；超时则返回当前状态（通常是排队重试中）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                ev = self._done.setdefault(task_id, asyncio.Event())
                ev.clear()
                task = self.get(task_id)
                left = deadline - loop.time()
                if task is None or task.status in ("success", "dead") or left <= 0:
                    return task
                try:
                    await asyncio.wait_for(ev.wait(), left)
                except asyncio.TimeoutError:
                    return self.get(task_id)
        finally:
            self._done.pop(task_id, None)

    # ---------------------------------------------------------------- 执行
    def _due(self) -> list[Task]:
        now = datetime.now()
        with self.Session() as s:
            q = (select(Task).where(Task.status == "pending", Task.next_run_at <= now)
                 .order_by(Task.next_run_at, Task.id))
            picked: dict[int, Task] = {}
            for t in s.scalars(q):
                if t.account_id not in picked and t.account_id not in self._busy:
                    picked[t.account_id] = t
            return list(picked.values())

    async def run_forever(self) -> None:
        self.recover()
        while not self._stopped:
            for task in self._due():
                self._busy.add(task.account_id)
                job = asyncio.create_task(self._execute(task.id))
                self._running.add(job)
                job.add_done_callback(self._running.discard)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _pending_before(self, when: datetime) -> bool:
        with self.Session() as s:
            return s.scalar(select(func.count()).select_from(Task)
                            .where(Task.status == "pending", Task.next_run_at <= when)) > 0

    async def drain(self, timeout: float = 30, horizon: float = 1.0) -> None:
        """测试辅助：执行到没有到期任务、且 horizon 秒内也没有待重试任务为止（风控暂停的任务不等待）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            due = self._due()
            if not due and not self._running and not self._pending_before(datetime.now() + timedelta(seconds=horizon)):
                return
            for task in due:
                self._busy.add(task.account_id)
                job = asyncio.create_task(self._execute(task.id))
                self._running.add(job)
                job.add_done_callback(self._running.discard)
            await asyncio.sleep(0.01)
        raise TimeoutError("队列未在时限内清空")

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def _save(self, task_id: int, **fields) -> None:
        with self.Session() as s:
            s.execute(update(Task).where(Task.id == task_id).values(updated_at=datetime.now(), **fields))
            s.commit()

    async def _execute(self, task_id: int) -> None:
        from ..services.accounts import AccountUnavailable

        task = self.get(task_id)
        try:
            if task is None or task.status != "pending":
                return
            self._save(task_id, status="running")
            t = self.tunables()
            try:
                rt = self.accounts.runtime(task.account_id)
                fn = OPS[task.op]
                result = await rt.run(task.op, lambda c: fn(c, **task.params))
            except AccountUnavailable as e:
                # 账号暂停：不消耗重试次数，等恢复时间再跑
                when = e.resume_at or datetime.now() + timedelta(seconds=30)
                if e.status == "disabled":
                    self._save(task_id, status="dead", last_error=str(e))
                else:
                    self._save(task_id, status="pending", next_run_at=when, last_error=str(e))
            except MtopError as e:
                err = e.ret[0] if e.ret else str(e)
                attempts = task.attempts + 1
                if e.kind is RetKind.RISK:
                    rt = self.accounts.runtime(task.account_id)
                    when = rt.resume_at or datetime.now() + timedelta(minutes=t.risk_pause_min)
                    self._save(task_id, status="pending", next_run_at=when, last_error=err)
                elif e.kind is RetKind.NETWORK and attempts < task.max_attempts:
                    delay = backoff_delay(attempts, t.backoff_base, t.backoff_cap)
                    self._save(task_id, status="pending", attempts=attempts, last_error=err,
                               next_run_at=datetime.now() + timedelta(seconds=delay))
                else:
                    self._save(task_id, status="dead", attempts=attempts, last_error=err)
            except Exception as e:  # noqa: BLE001 - 参数错误等，不重试
                log.exception("task %s failed", task_id)
                self._save(task_id, status="dead", attempts=task.attempts + 1, last_error=f"{type(e).__name__}: {e}")
            else:
                self._save(task_id, status="success", attempts=task.attempts + 1, result=to_jsonable(result),
                           last_error="")
                self.accounts.runtime(task.account_id).invalidate()
        finally:
            if task is not None:
                self._busy.discard(task.account_id)
            ev = self._done.get(task_id)
            if ev:
                ev.set()
            self._wake.set()
