"""集成测试：账号运行时状态机、任务队列、自动回复、统计（IT-01~04、IT-24~35）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from conftest import fast_tunables
from shopmgr.db import AutoReplyRule, Task
from shopmgr.services.accounts import AccountUnavailable
from shopmgr.web.app import Container
from shopmgr.xianyu import MockTransport, MtopError

pytestmark = pytest.mark.anyio

LOGIN = "mtop.taobao.idlemessage.pc.loginuser.get"
POLISH = "mtop.taobao.idle.item.polish"


async def test_it01_login_check(container, acc):
    """IT-01 登录态检测成功：状态正常并记录 userId"""
    aid, rt, t = acc
    a = container.accounts.get(aid)
    assert a.status == "ok" and a.xy_user_id == t.user_id and a.last_check_at


async def test_it02_session_expired(container, acc):
    """IT-02 注入 SESSION_EXPIRED → 失效；重新登录后恢复"""
    aid, rt, t = acc
    t.inject(LOGIN, "FAIL_SYS_SESSION_EXPIRED::Session过期")
    res = await container.accounts.check(aid)
    assert not res["ok"] and container.accounts.get(aid).status == "expired"
    assert (await container.accounts.check(aid))["ok"]
    assert container.accounts.get(aid).status == "ok"


async def test_it03_risk_pauses(container, acc):
    """IT-03 注入 RGV587 → 受限，约 10 分钟后恢复；期间调用被拦截"""
    aid, rt, t = acc
    t.inject(LOGIN, "RGV587_ERROR::SM::被挤爆啦")
    await container.accounts.check(aid)
    a = container.accounts.get(aid)
    assert a.status == "restricted"
    assert timedelta(minutes=9) < a.resume_at - datetime.now() <= timedelta(minutes=10)
    n = len(t.calls)
    with pytest.raises(AccountUnavailable):
        await rt.run("x", lambda c: c.item_list())
    assert len(t.calls) == n   # 暂停期间不发出任何请求


async def test_it04_risk_expires(container, acc):
    """IT-04 受限到期自动恢复；人工恢复也可"""
    aid, rt, t = acc
    t.inject(LOGIN, "RGV587_ERROR::SM")
    await container.accounts.check(aid)
    rt.resume_at = datetime.now() - timedelta(seconds=1)
    await rt.run("x", lambda c: c.item_list())
    assert container.accounts.get(aid).status == "ok"
    t.inject(LOGIN, "RGV587_ERROR::SM")
    await container.accounts.check(aid)
    rt.resume()
    assert container.accounts.get(aid).status == "ok"


async def test_block_disables(container, acc):
    """A4 FAIL_BIZ_BLOCK → 已停用"""
    aid, rt, t = acc
    t.inject(LOGIN, "FAIL_BIZ_BLOCK::封禁")
    await container.accounts.check(aid)
    assert container.accounts.get(aid).status == "disabled"


async def first_item(rt):
    return (await rt.client.item_list()).items[0].item_id


async def test_it24_success_path(container, acc):
    """IT-24 队列成功路径"""
    aid, rt, t = acc
    task = container.queue.enqueue(aid, "item.polish", {"item_id": await first_item(rt)})
    assert task.status == "pending"
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("success", 1)


async def test_it25_retry_then_success(container, acc):
    """IT-25 网络类错误重试，第 3 次成功"""
    aid, rt, t = acc
    t.inject(POLISH, "BRIDGE_ERROR::页面导航中断", times=2)
    task = container.queue.enqueue(aid, "item.polish", {"item_id": await first_item(rt)})
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("success", 3)


async def test_it26_dead_after_max(container, acc):
    """IT-26 超过重试上限进入死信"""
    aid, rt, t = acc
    t.inject(POLISH, "FAIL_SYS_SERVICE_TIMEOUT::超时", times=5)
    task = container.queue.enqueue(aid, "item.polish", {"item_id": await first_item(rt)})
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("dead", 3) and "FAIL_SYS_SERVICE_TIMEOUT" in task.last_error


async def test_it27_biz_error_no_retry(container, acc):
    """IT-27 业务错误不重试"""
    aid, rt, t = acc
    task = container.queue.enqueue(aid, "logistic.consign_dummy", {"order_id": "1", "text": "x"})
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("dead", 1)
    assert t.count("mtop.taobao.idle.logistic.consign.dummy") == 1


async def test_it28_risk_requeues_without_attempt(container, acc):
    """IT-28 风控命中：任务回到 pending，不计尝试次数，延后到恢复时间"""
    aid, rt, t = acc
    t.inject(POLISH, "RGV587_ERROR::SM")
    task = container.queue.enqueue(aid, "item.polish", {"item_id": await first_item(rt)})
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("pending", 0)
    assert task.next_run_at >= container.accounts.get(aid).resume_at - timedelta(seconds=1)
    # 到期后自动执行成功
    rt.resume_at = datetime.now() - timedelta(seconds=1)
    with container.Session() as s:
        s.get(Task, task.id).next_run_at = datetime.now()
        s.commit()
    await container.queue.drain()
    assert container.queue.get(task.id).status == "success"


async def test_it29_idempotent(container, acc):
    """IT-29 相同 idem_key 返回同一任务；已成功的不会重复执行"""
    aid, rt, t = acc
    oid = (await rt.client.wait_consign_list()).orders[0].order_id
    t1 = container.queue.enqueue(aid, "logistic.consign_dummy", {"order_id": oid, "text": "x"}, idem_key=f"ship:{oid}")
    t2 = container.queue.enqueue(aid, "logistic.consign_dummy", {"order_id": oid, "text": "x"}, idem_key=f"ship:{oid}")
    assert t1.id == t2.id
    await container.queue.drain()
    t3 = container.queue.enqueue(aid, "logistic.consign_dummy", {"order_id": oid, "text": "x"}, idem_key=f"ship:{oid}")
    await container.queue.drain()
    assert t3.id == t1.id and t.count("mtop.taobao.idle.logistic.consign.dummy") == 1


async def test_it30_manual_retry(container, acc):
    """IT-30 死信人工重试后成功"""
    aid, rt, t = acc
    t.inject(POLISH, "FAIL_SYS_SERVICE_TIMEOUT::超时", times=3)
    task = container.queue.enqueue(aid, "item.polish", {"item_id": await first_item(rt)})
    await container.queue.drain()
    assert container.queue.get(task.id).status == "dead"
    container.queue.retry(task.id)
    await container.queue.drain()
    task = container.queue.get(task.id)
    assert (task.status, task.attempts) == ("success", 1)
    with pytest.raises(ValueError):
        container.queue.retry(task.id)


async def test_it31_batch(container, acc):
    """IT-31 批次：拆单品任务全部成功"""
    aid, rt, t = acc
    orders = (await rt.client.wait_consign_list()).orders
    jobs = [("logistic.consign_dummy", {"order_id": o.order_id, "text": "卡密"}, o.order_id) for o in orders]
    batch = container.queue.create_batch(aid, "ship", "批量发货", jobs)
    await container.queue.drain()
    b = next(x for x in container.queue.batches() if x["id"] == batch.id)
    assert (b["total"], b["success"], b["dead"], b["pending"]) == (len(orders), len(orders), 0, 0)
    assert (await rt.client.wait_consign_list()).total == 0


async def test_it32_serial_per_account(settings):
    """IT-32 同账号串行、跨账号并行"""
    c = Container(settings,
                  transport_factory=lambda acc: MockTransport(user_id=str(2200000000 + acc.id), seed=acc.id),
                  tunables_override=fast_tunables())
    spans: dict[int, list] = {}
    a1, a2 = c.accounts.create("A"), c.accounts.create("B")
    for a in (a1, a2):
        await c.accounts.check(a.id)
        rt = c.accounts.runtime(a.id)
        rt.transport.latency = 0.05
        orig = rt.transport.mtop

        async def timed(api, v, data, _orig=orig, _id=a.id):
            start = asyncio.get_running_loop().time()
            res = await _orig(api, v, data)
            spans.setdefault(_id, []).append((start, asyncio.get_running_loop().time()))
            return res
        rt.transport.mtop = timed
        iid = (await rt.client.item_list()).items[0].item_id
        spans.clear()
        for _ in range(5):
            c.queue.enqueue(a.id, "item.polish", {"item_id": iid})
    t0 = asyncio.get_running_loop().time()
    await c.queue.drain()
    elapsed = asyncio.get_running_loop().time() - t0
    for sid, s in spans.items():
        s.sort()
        assert len(s) == 5
        assert all(s[i][1] <= s[i + 1][0] + 1e-6 for i in range(4)), "同账号请求出现重叠"
    assert elapsed < 0.05 * 10 * 0.9, "两个账号应并行执行"


async def test_it33_recover_after_restart(settings, acc, container):
    """IT-33 进程重启：running 任务回到 pending"""
    aid, rt, t = acc
    with container.Session() as s:
        s.add(Task(account_id=aid, op="item.polish", params={"item_id": "1"}, status="running"))
        s.commit()
    fresh = Container(settings, tunables_override=fast_tunables())
    assert fresh.queue.recover() == 1
    assert fresh.queue.list(status="running") == []


async def test_it34_autoreply(container, acc):
    """IT-34 自动回复：命中关键词入队发送；冷却期内不重复；自己发的消息不触发"""
    aid, rt, t = acc
    with container.Session() as s:
        s.add(AutoReplyRule(keyword="还在", match_type="contains", reply="在的亲，可以直接拍"))
        s.commit()
    n = await container.autoreply.scan_account(aid)
    assert n == 2          # Mock 中两个会话的最新消息为买家"你好，还在吗？"且未读
    await container.queue.drain()
    conv_ids = [c for c, v in t.convs.items() if v["messages"][-1]["content"]["custom"]["summary"] == "在的亲，可以直接拍"]
    assert len(conv_ids) == 2
    # 买家再发一条命中消息：冷却期内不回复
    t.buyer_says(conv_ids[0], "还在吗？？")
    assert await container.autoreply.scan_account(aid) == 0


async def test_it35_health_stats(container, acc):
    """IT-35 健康度统计与调用日志一致"""
    aid, rt, t = acc
    await rt.run("a", lambda c: c.item_list())
    t.inject("mtop.idle.web.xyh.item.list", "RGV587_ERROR::SM")
    with pytest.raises(MtopError):
        await rt.run("b", lambda c: c.item_list())
    h = next(x for x in container.stats.health() if x["account_id"] == aid)
    # check(登录态 + 资料接口 共 2 次成功) + 1 成功 + 1 风控
    assert (h["calls_24h"], h["risk_24h"], h["success_rate"]) == (4, 1, 75.0)
    assert h["status"] == "restricted" and "RGV587" in h["last_error"]


async def test_read_cache_and_singleflight(settings):
    """NFR 读缓存与并发合并：同 key 并发只发一次请求"""
    c = Container(settings,
                  transport_factory=lambda acc: MockTransport(user_id=str(2200000000 + acc.id), seed=acc.id),
                  tunables_override=fast_tunables(read_cache_s=30))
    a = c.accounts.create("A")
    await c.accounts.check(a.id)
    rt = c.accounts.runtime(a.id)
    rt.transport.latency = 0.05
    res = await asyncio.gather(*[rt.run("l", lambda cl: cl.item_list(), cache_key="items") for _ in range(5)])
    assert len({id(r) for r in res}) == 1
    assert rt.transport.count("mtop.idle.web.xyh.item.list") == 1
    await rt.run("l", lambda cl: cl.item_list(), cache_key="items")
    assert rt.transport.count("mtop.idle.web.xyh.item.list") == 1


async def test_rate_limit_applies(settings):
    """NFR 默认参数下单账号速率 ≤ 0.3 QPS（突发 2 之后开始等待）"""
    c = Container(settings)                       # 使用真实默认参数
    a = c.accounts.create("A")
    rt = c.accounts.runtime(a.id)
    waits = [rt.bucket.reserve() for _ in range(4)]
    assert waits[:2] == [0, 0]
    assert waits[2] == pytest.approx(1 / 0.3, rel=1e-4) and waits[3] == pytest.approx(2 / 0.3, rel=1e-4)
    assert c.tunables().qps == 0.3
