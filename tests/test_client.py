"""集成测试：接口封装层 + Mock 网关（IT-05 ~ IT-23），以及解析器对实测脱敏样本的兼容性。"""
from __future__ import annotations

import pytest

from conftest import FixtureTransport, load
from shopmgr.xianyu import MockTransport, MtopError, RetKind, XianyuClient
from shopmgr.xianyu.models import PublishForm, RateForm

pytestmark = pytest.mark.anyio


@pytest.fixture
async def cli():
    c = XianyuClient(MockTransport())
    await c.login_user()
    return c


def form(**kw) -> PublishForm:
    base = dict(title="测试商品", desc="描述", price=12.5, images=["https://img/1.jpg"])
    base.update(kw)
    return PublishForm(**base)


async def test_it05_item_list(cli):
    """IT-05 商品列表分页与分组"""
    p = await cli.item_list(page=1, page_size=4)
    assert len(p.items) == 4 and p.total == 6 and p.has_next
    assert {g.name for g in p.groups} >= {"综合", "在售", "已下架"}
    p2 = await cli.item_list(page=1, page_size=20, group_name="已下架")
    assert all(i.status == 2 for i in p2.items)


async def test_it06_publish(cli):
    """IT-06 发布商品：带 uniqueCode，列表可见；同 uniqueCode 重发不重复建品"""
    iid = await cli.publish(form(title="新发布的键盘"), unique_code="u-1")
    again = await cli.publish(form(title="新发布的键盘"), unique_code="u-1")
    assert iid and iid == again
    sent = [d for a, d in cli.t.calls if a == "mtop.idle.pc.idleitem.publish"][0]
    assert sent["uniqueCode"] == "u-1" and sent["price"] == "1250" and sent["bizcode"] == "pcMainPublish"
    titles = [i.title for i in (await cli.item_list(page_size=50)).items]
    assert titles.count("新发布的键盘") == 1


async def test_it06b_publish_requires_image(cli):
    """IT-06b 无图发布被平台拒绝，按 BIZ 分类"""
    with pytest.raises(MtopError) as e:
        await cli.publish(form(images=[]))
    assert e.value.kind is RetKind.BIZ


async def test_it07_badwords(cli):
    """IT-07 违禁词校验"""
    assert await cli.badwords_check("加微信优惠") == ["微信"]
    assert await cli.badwords_check("正常标题") == []


async def test_it08_open_cancel(cli):
    """IT-08 下架后上架"""
    iid = (await cli.item_list()).items[0].item_id
    await cli.item_cancel(iid)
    assert next(i for i in (await cli.item_list(page_size=50)).items if i.item_id == iid).status == 2
    await cli.item_open(iid)
    assert next(i for i in (await cli.item_list(page_size=50)).items if i.item_id == iid).status == 0


async def test_it09_polish(cli):
    """IT-09 擦亮：在售成功，下架商品报业务错误"""
    items = (await cli.item_list(page_size=50)).items
    await cli.item_polish(next(i for i in items if i.status == 0).item_id)
    with pytest.raises(MtopError):
        await cli.item_polish(next(i for i in items if i.status == 2).item_id)


async def test_it10_edit_price(cli):
    """IT-10 editDetail 回填 → edit 改价"""
    iid = (await cli.item_list()).items[0].item_id
    await cli.edit_price(iid, 99.9)
    assert next(i for i in (await cli.item_list(page_size=50)).items if i.item_id == iid).price == "99.90"


async def test_it11_drafts(cli):
    """IT-11 草稿保存 / 列表 / 发布 / 删除"""
    d1 = await cli.draft_save(form(title="草稿一"))
    d2 = await cli.draft_save(form(title="草稿二"))
    assert {d.draft_id for d in await cli.draft_list()} == {d1, d2}
    iid = await cli.draft_publish(d1)
    assert iid
    await cli.draft_delete(d2)
    assert await cli.draft_list() == []


async def test_it12_wait_consign(cli):
    """IT-12 待发货数量与 Mock 店铺一致"""
    p = await cli.wait_consign_list()
    assert p.total == len(p.orders) == len(cli.t.wait_orders)
    assert all(len(o.order_id) == 19 for o in p.orders)


async def test_it13_consign_dummy(cli):
    """IT-13 虚拟发货：返回发货单号（≠ 订单号），订单离开待发货，详情为已发货"""
    oid = (await cli.wait_consign_list()).orders[0].order_id
    res = await cli.consign_dummy(oid, "卡密：XXXX")
    assert res.consign_id and res.consign_id != oid
    assert oid not in {o.order_id for o in (await cli.wait_consign_list()).orders}
    assert (await cli.order_detail(oid)).status_title == "卖家已发货"
    sent = [d for a, d in cli.t.calls if a == "mtop.taobao.idle.logistic.consign.dummy"][0]
    assert sent == {"orderId": oid, "tradeText": "卡密：XXXX", "picList": "[]", "newUnconsign": True, "source": "normal"}


async def test_it13b_consign_dummy_requires_consign_id():
    """IT-13b 平台返回 SUCCESS 但没有 data.data.orderId 时判定为失败"""
    t = FixtureTransport({"mtop.taobao.idle.logistic.consign.dummy": {"ret": ["SUCCESS::调用成功"], "data": {"data": {}}}})
    with pytest.raises(MtopError):
        await XianyuClient(t).consign_dummy("1", "x")


async def test_it14_offline_ship(cli):
    """IT-14 运单识别 + 实物发货"""
    g = await cli.guess_mailno("SF1234567890")
    assert (g.cp_code, g.cp_name) == ("SF", "顺丰速运")
    oid = (await cli.wait_consign_list()).orders[0].order_id
    await cli.consign_offline(oid, "SF1234567890", g.cp_code, g.brand_code)
    assert oid in cli.t.shipped


async def test_it15_order_detail(cli):
    """IT-15 订单详情：状态标题与按钮"""
    oid = (await cli.wait_consign_list()).orders[0].order_id
    d = await cli.order_detail(oid)
    assert d.order_id == oid and d.status_title.startswith("买家已付款")
    assert [b.action for b in d.buttons] == ["CONSIGN", "MODIFY_PRICE"]


async def test_it16_trade_actions(cli):
    """IT-16 改价 / 关闭原因 / 关闭 / 延长收货"""
    orders = (await cli.wait_consign_list()).orders
    await cli.adjust_price(orders[0].order_id, 8.8)
    assert cli.t.wait_orders[orders[0].order_id]["value"] == 880
    reasons = await cli.close_reasons(orders[1].order_id)
    assert reasons and reasons[0].text
    await cli.close_by_seller(orders[1].order_id, reasons[0].code)
    assert (await cli.order_detail(orders[1].order_id)).status_title == "交易关闭"
    await cli.consign_dummy(orders[2].order_id, "x")
    await cli.delay_confirm(orders[2].order_id, 3)


async def test_it17_conversations_messages(cli):
    """IT-17 会话与消息"""
    page = await cli.conversations()
    assert page.conversations and all(c.cid and c.peer_id != cli.self_id for c in page.conversations)
    conv = next(c for c in page.conversations if c.unread)
    msgs = await cli.messages(conv.cid)
    assert msgs.messages[0].text == "你好，还在吗？" and msgs.messages[0].sender_id == conv.peer_id


async def test_it18_send_and_readback(cli):
    """IT-18 发送文本后回读"""
    conv = (await cli.conversations()).conversations[0]
    await cli.send_text(conv.cid, conv.peer_id, "在的，欢迎下单 👍")
    last = (await cli.messages(conv.cid)).messages[-1]
    assert (last.text, last.sender_id) == ("在的，欢迎下单 👍", cli.self_id)


async def test_it19_redpoint_quickreply_card_blacklist(cli):
    """IT-19 红点 / 快捷回复 / 商品卡片 / 黑名单"""
    assert await cli.redpoint() == 2
    assert len(await cli.quick_replies()) == 3
    conv = (await cli.conversations()).conversations[0]
    await cli.send_card(conv.cid, conv.item_id)
    assert (await cli.messages(conv.cid)).messages[-1].text.startswith("[商品卡片]")
    assert await cli.blacklist_query(conv.cid) is False
    await cli.blacklist_add(conv.cid)
    assert await cli.blacklist_query(conv.cid) is True
    await cli.blacklist_remove(conv.cid)
    assert await cli.blacklist_query(conv.cid) is False


async def test_it20_refunds(cli):
    """IT-20 退款列表与详情"""
    page = await cli.refund_list()
    assert len(page.refunds) == 3
    pending = next(r for r in page.refunds if r.status == 1)
    d = await cli.refund_detail(pending.refund_id, pending.order_id)
    assert d.status_text == "待卖家处理" and d.amount == pending.amount
    assert {a.name for a in d.actions} == {"同意退款", "拒绝退款", "联系买家"}


async def test_it21_refund_dynamic_action(cli):
    """IT-21 按服务端下发的 apiName 动态调用"""
    r = next(r for r in (await cli.refund_list()).refunds if r.status == 1)
    refuse = next(a for a in (await cli.refund_detail(r.refund_id, r.order_id)).actions if a.name == "拒绝退款")
    await cli.refund_action(refuse)
    assert cli.t.calls[-1][0] == "mtop.taobao.idle.refund.seller.refuse"
    assert (await cli.refund_detail(r.refund_id, r.order_id)).status == 6


async def test_it22_agree_refund_is_manual(cli):
    """IT-22 同意退款不经程序调用，只打开官方页面"""
    r = next(r for r in (await cli.refund_list()).refunds if r.status == 1)
    agree = next(a for a in (await cli.refund_detail(r.refund_id, r.order_id)).actions if a.name == "同意退款")
    with pytest.raises(ValueError):
        await cli.refund_action(agree)
    assert cli.t.count("mtop.taobao.idle.refund.agree.refund") == 0
    url = await cli.open_refund_page(r.refund_id, r.order_id)
    assert cli.t.opened == [url] and r.refund_id in url


async def test_it23_rate(cli):
    """IT-23 提交评价后列表可查"""
    before = len((await cli.rate_list()).rates)
    await cli.rate_create(RateForm(trade_id="3316400474147019790", rate=1, feedback="合作愉快", tags=["发货快"]))
    after = await cli.rate_list()
    assert len(after.rates) == before + 1 and after.rates[0].feedback == "合作愉快"
    sent = [d for a, d in cli.t.calls if a == "mtop.taobao.idle.rate.create"][0]
    assert sent["rate"] == 1 and sent["tradeId"] == "3316400474147019790"


async def test_upload_too_small(cli):
    """B1 补充：过小图片被拒（PICTURE_NOT_SAFE）"""
    with pytest.raises(MtopError) as e:
        await cli.upload_image("a.png", b"x" * 10)
    assert e.value.code == "PICTURE_NOT_SAFE"


async def test_login_expired():
    """A2 登录失效分类"""
    t = MockTransport()
    t.logged_in = False
    with pytest.raises(MtopError) as e:
        await XianyuClient(t).login_user()
    assert e.value.kind is RetKind.SESSION


# ---------------------------------------------------------------- 实测脱敏样本
async def test_fixture_parsers():
    """解析器兼容实测结构：商品、订单详情、退款详情、评价、会话、消息、发货结果"""
    t = FixtureTransport({
        "mtop.idle.web.xyh.item.list": load("item_list.json"),
        "mtop.idle.web.trade.order.detail": load("order_detail.json"),
        "mtop.taobao.idle.refund.detail": load("refund_detail.json"),
        "mtop.idle.web.trade.rate.list": load("rate_list.json"),
        "/r/Conversation/listNewestPagination": load("conv_list.json"),
        "/r/MessageManager/listUserMessages": load("messages.json"),
        "mtop.taobao.idle.logistic.consign.dummy": load("ship_result.json"),
    })
    c = XianyuClient(t)
    items = await c.item_list()
    assert len(items.items) == 3 and items.items[0].item_id.isdigit() and items.groups
    od = await c.order_detail("3316400474147019790")
    assert od.status_title == "交易成功" and {b.action for b in od.buttons} >= {"RATE"}
    rd = await c.refund_detail("290891714435569407", "3316978525130041951")
    assert rd.status == 5 and rd.idle_status == "040" and rd.status_text
    rates = await c.rate_list()
    assert rates.rates and rates.rates[0].rate == 1
    convs = await c.conversations()
    assert len(convs.conversations) == 2 and convs.conversations[0].cid.isdigit()
    msgs = await c.messages(convs.conversations[0].cid)
    assert msgs.messages and msgs.messages[0].created <= msgs.messages[-1].created
    ship = await c.consign_dummy("1", "x")
    assert ship.consign_id.isdigit()
