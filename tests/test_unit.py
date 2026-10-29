"""单元测试 UT-01 ~ UT-15。"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import time

import pytest

from conftest import FixtureTransport, load
from shopmgr.db import AutoReplyRule
from shopmgr.scheduler.backoff import backoff_delay
from shopmgr.scheduler.ratelimit import TokenBucket
from shopmgr.services.autoreply import match_rule
from shopmgr.xianyu import XianyuClient, im
from shopmgr.xianyu.errors import RetKind, classify
from shopmgr.xianyu.sign import mtop_sign, parse_h5_tk

pytestmark = pytest.mark.anyio


def test_ut01_sign_formula():
    """UT-01 签名公式 md5(token&t&appKey&data)"""
    data = '{"itemId":"1"}'
    expect = hashlib.md5(f"tok&1700000000000&34839810&{data}".encode()).hexdigest()
    assert mtop_sign("tok", "1700000000000", "34839810", data) == expect


def test_ut02_parse_h5_tk():
    """UT-02 _m_h5_tk 解析"""
    assert parse_h5_tk("abc_123") == ("abc", "123")
    assert parse_h5_tk("") == ("", "")
    assert parse_h5_tk("noexpire") == ("noexpire", "")


@pytest.mark.parametrize("ret,kind", [
    (["SUCCESS::调用成功"], RetKind.OK),
    (["FAIL_SYS_SESSION_EXPIRED::Session过期"], RetKind.SESSION),
    (["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], RetKind.SESSION),
    (["RGV587_ERROR::SM::被挤爆啦"], RetKind.RISK),
    (["FAIL_SYS_USER_VALIDATE::需验证"], RetKind.RISK),
    (["FAIL_SYS_TRAFFIC_LIMIT::限流"], RetKind.RISK),
    (["FAIL_BIZ_BLOCK::封禁"], RetKind.BLOCK),
    (["BIZ_ORDER_NOT_FOUND::订单不存在"], RetKind.BIZ),
    (["BRIDGE_ERROR::page closed"], RetKind.NETWORK),
])
def test_ut03_classify(ret, kind):
    """UT-03 ret 码分类"""
    assert classify(ret) is kind


def test_ut04_classify_empty():
    """UT-04 ret 为空视为网络/桥接错误"""
    assert classify(None) is RetKind.NETWORK
    assert classify([]) is RetKind.NETWORK


def test_ut05_text_message_fields():
    """UT-05 发文本消息模型字段（实测确认的字段写法）"""
    model, scope = im.build_text_message("68032467466", "应该的！", "3395440095", "3977569407")
    assert model["cid"] == "68032467466@goofish"
    assert model["conversationType"] == 1
    assert model["content"]["contentType"] == 101
    custom = model["content"]["custom"]
    assert custom["type"] == 1 and "summary" not in custom
    assert model["redPointPolicy"] == 0
    assert model["extension"] == {"extJson": "{}"}
    assert model["ctx"] == {"appVersion": "1.0", "platform": "web"}
    assert model["msgReadStatusSetting"] == 1


def test_ut06_text_encoding_roundtrip():
    """UT-06 custom.data base64 编解码（中文 + emoji），与文档样本一致"""
    sample = "eyJjb250ZW50VHlwZSI6MSwidGV4dCI6eyJ0ZXh0Ijoi5bqU6K+l55qE77yBIn19"
    assert im.encode_text("应该的！") == sample
    assert im.decode_content({"contentType": 101, "custom": {"type": 1, "data": im.encode_text("感谢🥹")}}) \
        == ("text", "感谢🥹", None)
    raw = json.loads(base64.b64decode(sample))
    assert raw == {"contentType": 1, "text": {"text": "应该的！"}}


def test_ut07_uuid_format():
    """UT-07 uuid = '-' + 13 位毫秒时间戳 + 随机串"""
    assert re.fullmatch(r"-\d{13}\d+", im.make_uuid())


def test_ut08_mid_format():
    """UT-08 mid = 随机数+时间戳 + ' 0'"""
    assert re.fullmatch(r"\d{13,16} 0", im.make_mid())


def test_ut09_receivers():
    """UT-09 actualReceivers 必须同时含对方与自己且带域"""
    _, scope = im.build_text_message("1", "hi", "111", "222@goofish")
    assert scope == {"actualReceivers": ["111@goofish", "222@goofish"]}
    with pytest.raises(ValueError):
        im.build_text_message("1", "hi", "", "222")
    with pytest.raises(ValueError):
        im.build_text_message("1", "", "111", "222")


def test_ut10_token_bucket():
    """UT-10 令牌桶：QPS=0.3、容量 2 → 前 2 次立即，第 3 次需等待约 3.33s"""
    now = [0.0]
    b = TokenBucket(0.3, 2, clock=lambda: now[0])
    assert b.reserve() == 0 and b.reserve() == 0
    assert b.reserve() == pytest.approx(1 / 0.3, rel=1e-6)
    now[0] = 10.0   # 10 秒后补满（容量上限 2）
    assert b.reserve() == 0


def test_ut11_backoff():
    """UT-11 指数退避 1.5/3/6…封顶 60，抖动 0–20%"""
    zero = random.Random()
    zero.random = lambda: 0.0
    assert [backoff_delay(n, rnd=zero) for n in (1, 2, 3)] == [1.5, 3.0, 6.0]
    assert backoff_delay(10, rnd=zero) == 60
    for n in range(1, 8):
        d = backoff_delay(n)
        assert min(60, 1.5 * 2 ** (n - 1)) <= d <= min(60, 1.5 * 2 ** (n - 1) * 1.2)


async def test_ut12_order_id_precision():
    """UT-12 19 位订单号取 orderIdStr；实测样本里 orderId 数值已丢精度"""
    sample = load("bought_list.json")
    first = sample["data"]["items"][0]["commonData"]
    assert str(first["orderId"]) != first["orderIdStr"]           # 样本本身证明了精度问题
    client = XianyuClient(FixtureTransport({"mtop.idle.web.trade.bought.list": sample}))
    page = await client.bought_list()
    assert page.orders[0].order_id == first["orderIdStr"]
    assert all(isinstance(o.order_id, str) and len(o.order_id) == 19 for o in page.orders)


async def test_ut13_wait_consign_parse():
    """UT-13 待发货解析（实测结构）"""
    sample = load("wait_consign.json")
    client = XianyuClient(FixtureTransport({"mtop.taobao.idle.logistic.wait.consign.order.query": sample}))
    page = await client.wait_consign_list()
    groups = sample["data"]["data"]["consignOrderInfos"]
    assert len(page.orders) == sum(len(g["bizOrderInfoList"]) for g in groups) == 11
    o = page.orders[0]
    raw = groups[0]["bizOrderInfoList"][0]
    assert o.order_id == raw["bizOrderId"] and o.item_id == raw["auctionId"]
    assert o.amount == f"{raw['orderValue'] / 100:.2f}"
    assert not hasattr(o, "receiver") and "mobile" not in o.model_dump_json()


def test_ut14_refund_actions():
    """UT-14 退款详情 bottomBar 按钮解析"""
    acts = XianyuClient.parse_refund_actions([
        {"code": "refuse", "name": "拒绝退款", "clickEvent": {"type": "api", "data": {
            "apiName": "mtop.taobao.idle.refund.seller.refuse", "apiVersion": "2.0", "params": {"refundId": "1"}}}},
        {"code": "contact", "name": "联系卖家", "clickEvent": {"type": "url", "data": {"url": "https://x"}}},
        {"code": "x", "name": "其它"},
    ])
    assert (acts[0].kind, acts[0].api_name, acts[0].api_version, acts[0].params) == \
        ("api", "mtop.taobao.idle.refund.seller.refuse", "2.0", {"refundId": "1"})
    assert (acts[1].kind, acts[1].url) == ("url", "https://x")
    assert acts[2].kind == "other"


def test_ut15_autoreply_match():
    """UT-15 自动回复规则匹配"""
    rules = [
        AutoReplyRule(id=1, keyword="在吗", match_type="contains", reply="在的", enabled=True, sort=0),
        AutoReplyRule(id=2, keyword="包邮", match_type="exact", reply="不包邮哦", enabled=True, sort=0),
        AutoReplyRule(id=3, keyword=r"多少钱|价格", match_type="regex", reply="看标价", enabled=True, sort=0),
        AutoReplyRule(id=4, keyword="(", match_type="regex", reply="坏正则", enabled=True, sort=0),
        AutoReplyRule(id=5, keyword="发货", match_type="contains", reply="已停用", enabled=False, sort=0),
    ]
    assert match_rule("你好在吗", rules).id == 1
    assert match_rule("包邮", rules).id == 2
    assert match_rule("包邮吗", rules) is None
    assert match_rule("这个多少钱", rules).id == 3
    assert match_rule("什么时候发货", rules) is None
    assert match_rule("", rules) is None


def test_ut16_fingerprint():
    """UT-16 指纹伪装：同账号稳定、不同账号不同、UA 不含 HeadlessChrome"""
    from shopmgr.xianyu.stealth import JS_STEALTH, make_fingerprint, viewport_of

    a1, a1_again, a2 = make_fingerprint(1), make_fingerprint(1), make_fingerprint(2)
    assert a1 == a1_again, "同一账号必须得到同一份指纹"
    assert a1 != a2, "不同账号指纹不应完全相同"

    for fp in (a1, a2):
        assert "HeadlessChrome" not in fp["ua"] and "Chrome/" in fp["ua"]
        assert fp["platform"] == "Win32" and fp["languages"][0] == "zh-CN"
        assert fp["hardware_concurrency"] in (4, 8, 12, 16)
        assert fp["device_memory"] in (4, 8, 16)
        assert fp["max_touch_points"] == 0
        assert "ANGLE (" in fp["webgl_renderer"] and fp["webgl_vendor"].startswith("Google Inc.")
        w, h = fp["screen"]
        assert w >= 1366 and h >= 768 and fp["dpr"] in (1, 1.25)

    vp = viewport_of(a1)
    assert vp["width"] == a1["screen"][0] and vp["height"] < a1["screen"][1]
    assert "__FP__" in JS_STEALTH and "webdriver" in JS_STEALTH


def test_ut17_image_url():
    """UT-17 商品封面地址归一化与兜底"""
    from shopmgr.xianyu.client import first_image, img_url

    assert img_url("//img.alicdn.com/a.jpg") == "https://img.alicdn.com/a.jpg"
    assert img_url("http://img.alicdn.com/a.jpg") == "https://img.alicdn.com/a.jpg"
    assert img_url("https://img.alicdn.com/a.jpg") == "https://img.alicdn.com/a.jpg"
    assert img_url("") == "" and img_url(None) == ""

    infos = json.dumps([{"url": "//a/1.jpg", "major": False}, {"url": "//a/2.jpg", "major": True}])
    assert first_image({"imageInfos": infos}) == "https://a/2.jpg"    # 优先 major 那张，且已归一化
    assert first_image({"imageInfos": [{"url": "http://a/3.jpg"}]}) == "https://a/3.jpg"
    assert first_image({"imageInfos": "不是 JSON"}) == ""
    assert first_image({}) == ""


def test_ut18_cookie_snapshot(tmp_path):
    """UT-18 登录态快照：session cookie 补远期有效期，过期条目丢弃，只存闲鱼系域名"""
    from shopmgr.xianyu.transport_browser import BrowserTransport

    snap = tmp_path / "sessions" / "acc_1.json"
    tr = BrowserTransport(tmp_path / "profiles" / "acc_1", session_file=snap)

    raw = [
        # cookie2 是 session cookie（expires=-1），不补有效期就会随进程退出丢失
        {"name": "cookie2", "value": "abc", "domain": ".goofish.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "unb", "value": "3977569407", "domain": ".goofish.com", "path": "/",
         "expires": time.time() + 86400, "httpOnly": False, "secure": False},
        {"name": "已过期", "value": "x", "domain": ".goofish.com", "path": "/", "expires": time.time() - 10},
        {"name": "别家的", "value": "y", "domain": ".example.com", "path": "/", "expires": -1},
    ]
    assert tr.save_cookies(raw) == 2          # 过期条目和外部域名都不落盘

    rows = {c["name"]: c for c in tr.load_cookies()}
    assert set(rows) == {"cookie2", "unb"}
    assert rows["cookie2"]["expires"] > time.time() + 86400     # session cookie 已补有效期
    assert rows["cookie2"]["httpOnly"] is True and rows["cookie2"]["sameSite"] == "None"
    assert rows["unb"]["expires"] < time.time() + 90000          # 原有有效期不被改写

    # 原子写：不留下半截临时文件
    assert not list(snap.parent.glob("*.tmp"))
    # 快照损坏时返回空列表而不是抛异常
    snap.write_text("{ 坏掉的 json", encoding="utf-8")
    assert tr.load_cookies() == []


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode()


def _dec(obj, summary=""):
    return im.decode_content({"contentType": 101, "custom": {"summary": summary, "data": _b64(obj)}})


def test_ut19_card_content():
    """UT-19 非文本消息解析：五种真实 contentType 各走各的路径"""
    # contentType=7 商品卡 —— 图片字段是 mainPic（实测），不是 itemMainPic
    kind, text, card = _dec(
        {"contentType": 7, "itemCard": {"item": {"itemId": 1092984250845, "title": "安全启动接口加载",
                                                "mainPic": "http://img.alicdn.com/i1/a.jpg",
                                                "price": "20.00"}, "itemTip": "我想要"}},
        "[我已发货]")
    # 列表预览用卡片标题而不是 summary（summary 常是 "[卡片消息]" 这种兜底文案）
    assert (kind, text) == ("card", "安全启动接口加载")
    assert card == {"ct": 7, "title": "安全启动接口加载", "sub": "我想要", "price": "20.00",
                    "pic": "http://img.alicdn.com/i1/a.jpg", "item_id": "1092984250845",
                    "order_id": "", "action": ""}

    # contentType=14 系统提示 —— 是平台插话，不是卡片，必须单独成类才能居中渲染
    kind, text, card = _dec({"contentType": 14, "tip": {"tip": "你已确认收货，交易成功"}}, "[你已发货]")
    assert (kind, text, card) == ("tip", "你已确认收货，交易成功", None)

    # contentType=25 通知卡 —— 标题/副标题/按钮都在 dxCard.item.main.exContent
    kind, _, card = _dec({"contentType": 25, "dxCard": {"item": {"main": {"exContent": {
        "title": "快给ta一个评价吧～", "subTitle": "交易体验还满意吗？",
        "button": {"text": "去评价"}}}}}}, "通知")
    assert kind == "card"
    assert (card["title"], card["sub"], card["action"]) == ("快给ta一个评价吧～", "交易体验还满意吗？", "去评价")

    # contentType=26 商品动态卡 —— 字段埋在 descInfo 里，价格在 thirdLineText.price
    kind, _, card = _dec({"contentType": 26, "dxCard": {"item": {"main": {"exContent": {
        "descInfo": {"firstLineText": "nano banana2 AI生图神器", "imgUrl": "http://img.alicdn.com/b.jpg",
                     "thirdLineText": {"price": "1.00", "priceIdentifier": "¥"}}}}}}}, "我要咨询这个商品")
    assert kind == "card"
    assert (card["title"], card["pic"], card["price"]) == ("nano banana2 AI生图神器",
                                                          "http://img.alicdn.com/b.jpg", "1.00")

    # contentType=4 视频 —— 用首帧快照当封面，气泡里不加载 mp4
    kind, _, card = _dec({"contentType": 4, "video": {"snapshot": "https://img.alicdn.com/s.jpg",
                                                      "url": "https://x/v.mp4", "width": 1920}}, "[视频]")
    assert (kind, card["pic"]) == ("video", "https://img.alicdn.com/s.jpg")

    # 订单卡：不在精确分支里的类型仍按候选键递归找
    kind, _, card = _dec({"contentType": 13, "order": {"title": "已拍下", "orderId": "3316", "price": "20"}}, "订单")
    assert kind == "card" and card["order_id"] == "3316"

    # 认不出结构时不能变成"[非文本消息]"，要保留 summary
    assert _dec({"contentType": 99, "whatever": {"foo": "bar"}}, "系统提示")[:2] == ("type99", "系统提示")


def test_ut20_wait_consign_helpers():
    """UT-20 待发货判定与"空结果"识别

    真实接口（D-24）有两个坑：
    1. 没有待发货订单时返回 QUERY_SELLER_TRADE_ORDER_LIST_FAILED，不是空列表；
    2. 状态文案里"已发货，待买家确认收货"含"待…收货"，不能误判成待发货。
    """
    from shopmgr.xianyu.client import _is_empty_consign, _is_wait_consign, _order_item_info, _order_status_title
    from shopmgr.xianyu.errors import MtopError

    # 空结果要能识别，真失败不能吞
    assert _is_empty_consign(MtopError("x", ["QUERY_SELLER_TRADE_ORDER_LIST_FAILED::查询卖家订单列表失败"]))
    assert not _is_empty_consign(MtopError("x", ["FAIL_SYS_SESSION_EXPIRED::Session过期"]))
    assert not _is_empty_consign(MtopError("x", []))

    # 待发货判定
    assert _is_wait_consign("买家已付款，等待发货")
    assert _is_wait_consign("待发货")
    assert _is_wait_consign("等待卖家发货")
    assert not _is_wait_consign("已发货，待买家确认收货")     # 已发货，不是待发货
    assert not _is_wait_consign("交易成功")
    assert not _is_wait_consign("交易关闭，有退款")
    assert not _is_wait_consign("")

    # 状态文案两个位置都要吃：utArgs 优先，其次 components
    assert _order_status_title({"utArgs": {"orderMainTitle": "待发货"}}) == "待发货"
    assert _order_status_title(
        {"components": [{"data": {"orderStatusInfo": {"title": "交易成功"}}}]}) == "交易成功"
    assert _order_status_title({}) == ""

    # 商品信息块
    info = _order_item_info({"components": [
        {"data": {"orderStatusInfo": {"title": "x"}}},
        {"data": {"itemInfo": {"title": "测试商品", "price": "5.00", "buyAmount": "1",
                               "itemMainPictCdnUrl": "//img.alicdn.com/a.jpg"}}}]})
    assert info["title"] == "测试商品" and info["price"] == "5.00"
