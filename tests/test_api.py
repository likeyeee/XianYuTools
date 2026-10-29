"""接口测试：FastAPI 全链路（API-01 ~ API-14），后台队列真实运行。"""
from __future__ import annotations

import io
import time
import warnings

import pytest

from conftest import fast_tunables
from shopmgr.config import Settings
from shopmgr.web.app import create_app
from shopmgr.xianyu import MockTransport
from shopmgr.xianyu.errors import RetKind

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, open_browser=False, workers=True),
                     transport_factory=lambda acc: MockTransport(user_id=str(2200000000 + acc.id), seed=acc.id),
                     tunables_override=fast_tunables())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def aid(client):
    r = client.post("/api/accounts", json={"name": "店铺A"})
    assert r.status_code == 200 and r.json()["status"] == "new"
    account_id = r.json()["id"]
    assert client.post(f"/api/accounts/{account_id}/check").json()["ok"]
    return account_id


def wait_batch(client, batch_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        b = next(x for x in client.get("/api/batches").json() if x["id"] == batch_id)
        if b["pending"] == 0:
            return b
        time.sleep(0.05)
    raise AssertionError("批次未完成")


def png(w, h) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 50)).save(buf, "PNG")
    return buf.getvalue()


def test_api01_accounts(client, aid):
    """API-01 新增账号 → 列表"""
    rows = client.get("/api/accounts").json()
    assert [(r["id"], r["status"], r["status_text"]) for r in rows] == [(aid, "ok", "正常")]
    r = client.patch(f"/api/accounts/{aid}", json={"autoreply_enabled": True, "name": "改名"}).json()
    assert r["autoreply_enabled"] and r["name"] == "改名"
    assert client.post(f"/api/accounts/{aid}/check").json()["ok"]
    assert client.get("/api/a/999/items").status_code == 404


def test_api02_items_publish_batch(client, aid):
    """API-02 商品：上传 → 发布 → 单品下架 → 批量上架 / 改价"""
    bad = client.post(f"/api/a/{aid}/upload", files={"file": ("s.png", png(100, 100), "image/png")})
    assert bad.status_code == 400 and "过小" in bad.json()["detail"]
    url = client.post(f"/api/a/{aid}/upload", files={"file": ("ok.png", png(800, 800), "image/png")}).json()["url"]
    t = client.post(f"/api/a/{aid}/items/publish", json={"title": "新品", "price": 9.9, "images": [url]}).json()
    assert t["status"] == "success" and t["result"]["item_id"]
    no_img = client.post(f"/api/a/{aid}/items/publish", json={"title": "新品", "price": 9.9, "images": []})
    assert no_img.status_code == 400
    items = client.get(f"/api/a/{aid}/items?size=50").json()["items"]
    on = [i for i in items if i["status"] == 0]
    t = client.post(f"/api/a/{aid}/items/{on[0]['item_id']}/action", json={"action": "cancel"}).json()
    assert t["status"] == "success"
    r = client.post(f"/api/a/{aid}/items/batch", json={"action": "open", "item_ids": [i["item_id"] for i in items]}).json()
    b = wait_batch(client, r["batch_id"])
    assert b["success"] == len(items)
    assert all(i["status"] == 0 for i in client.get(f"/api/a/{aid}/items?size=50").json()["items"])
    ids = [i["item_id"] for i in items[:2]]
    r = client.post(f"/api/a/{aid}/items/batch", json={"action": "price", "item_ids": ids, "price_mode": "percent",
                                                        "value": 50, "prices": {i["item_id"]: i["price"] for i in items[:2]}}).json()
    wait_batch(client, r["batch_id"])
    now = {i["item_id"]: i["price"] for i in client.get(f"/api/a/{aid}/items?size=50").json()["items"]}
    assert all(float(now[i["item_id"]]) == pytest.approx(float(i["price"]) / 2, abs=0.01) for i in items[:2])


def test_api03_ship_dummy(client, aid):
    """API-03 待发货 → 虚拟发货（同步等待）"""
    w = client.get(f"/api/a/{aid}/orders/wait-consign").json()
    oid = w["orders"][0]["order_id"]
    t = client.post(f"/api/a/{aid}/orders/{oid}/ship-dummy", json={"text": "卡密 ABCD"}).json()
    assert t["status"] == "success" and t["result"]["consign_id"] != oid
    assert client.get(f"/api/a/{aid}/orders/wait-consign").json()["total"] == w["total"] - 1
    # 重复提交同一订单发货：返回原任务，不重复调用
    again = client.post(f"/api/a/{aid}/orders/{oid}/ship-dummy", json={"text": "卡密 ABCD"}).json()
    assert again["id"] == t["id"]


def test_api04_batch_ship(client, aid):
    """API-04 批量发货"""
    ids = [o["order_id"] for o in client.get(f"/api/a/{aid}/orders/wait-consign").json()["orders"]]
    r = client.post(f"/api/a/{aid}/orders/batch-ship", json={"order_ids": ids + ids[:1], "text": "统一交付"}).json()
    assert r["total"] == len(ids)            # 重复订单号去重
    assert wait_batch(client, r["batch_id"])["success"] == len(ids)
    assert client.get(f"/api/a/{aid}/orders/wait-consign").json()["total"] == 0


def test_api05_ship_offline(client, aid):
    """API-05 运单识别 → 实物发货；订单详情 / 改价 / 关闭"""
    orders = client.get(f"/api/a/{aid}/orders/wait-consign").json()["orders"]
    g = client.get(f"/api/a/{aid}/mailno/guess", params={"mail_no": "YT9876543210"}).json()
    assert g["cp_code"] == "YTO"
    t = client.post(f"/api/a/{aid}/orders/{orders[0]['order_id']}/ship-offline", json={"mail_no": "YT9876543210"}).json()
    assert t["status"] == "success" and t["result"]["cp_code"] == "YTO"
    d = client.get(f"/api/a/{aid}/orders/{orders[0]['order_id']}").json()
    assert d["status_title"] == "卖家已发货"
    assert client.post(f"/api/a/{aid}/orders/{orders[1]['order_id']}/adjust-price", json={"price": 5}).json()["status"] == "success"
    reason = client.get(f"/api/a/{aid}/orders/{orders[2]['order_id']}/close-reasons").json()[0]["code"]
    assert client.post(f"/api/a/{aid}/orders/{orders[2]['order_id']}/close", json={"reason": reason}).json()["status"] == "success"
    assert client.post(f"/api/a/{aid}/orders/{orders[0]['order_id']}/delay", json={"days": 3}).json()["status"] == "success"


def test_api06_im(client, aid):
    """API-06 会话 → 消息 → 发送 → 回读；卡片、快捷回复、黑名单"""
    conv = client.get(f"/api/a/{aid}/im/conversations").json()["conversations"][0]
    t = client.post(f"/api/a/{aid}/im/send", json={"cid": conv["cid"], "peer_id": conv["peer_id"], "text": "<b>你好</b>"}).json()
    assert t["status"] == "success"
    msgs = client.get(f"/api/a/{aid}/im/messages", params={"cid": conv["cid"]}).json()["messages"]
    assert msgs[-1]["text"] == "<b>你好</b>"       # 原样存取，前端负责转义
    assert client.post(f"/api/a/{aid}/im/card", json={"cid": conv["cid"], "item_id": conv["item_id"]}).json()["status"] == "success"
    client.post("/api/quick-phrases", json={"text": "本地短语"})
    q = client.get(f"/api/a/{aid}/im/quickreplies").json()
    assert q["platform"] and q["local"][0]["text"] == "本地短语"
    assert client.post(f"/api/a/{aid}/im/blacklist", json={"cid": conv["cid"], "block": True}).json()["status"] == "success"
    assert client.get(f"/api/a/{aid}/im/blacklist", params={"cid": conv["cid"]}).json()["blocked"] is True
    assert client.get(f"/api/a/{aid}/im/redpoint").json()["unread"] >= 0


def test_api07_refunds(client, aid):
    """API-07 退款：列表 / 详情 / 动态按钮 / 同意退款只能打开官方页 / 客服介入"""
    refunds = client.get(f"/api/a/{aid}/refunds").json()["refunds"]
    r = next(x for x in refunds if x["status"] == 1)
    d = client.get(f"/api/a/{aid}/refunds/{r['refund_id']}", params={"order_id": r["order_id"]}).json()
    agree = next(a for a in d["actions"] if a["name"] == "同意退款")
    refuse = next(a for a in d["actions"] if a["name"] == "拒绝退款")
    blocked = client.post(f"/api/a/{aid}/refunds/{r['refund_id']}/action", json=agree)
    assert blocked.status_code == 400 and "支付密码" in blocked.json()["detail"]
    opened = client.post(f"/api/a/{aid}/refunds/{r['refund_id']}/open", params={"order_id": r["order_id"]}).json()
    assert "refundDetail" in opened["url"]
    assert client.post(f"/api/a/{aid}/refunds/{r['refund_id']}/action", json=refuse).json()["status"] == "success"
    t = client.post(f"/api/a/{aid}/refunds/{r['refund_id']}/xiaoer", json={"order_id": r["order_id"], "content": "请介入"}).json()
    assert t["status"] == "success"


def test_api08_rates(client, aid):
    """API-08 评价"""
    n = len(client.get(f"/api/a/{aid}/rates").json()["rates"])
    t = client.post(f"/api/a/{aid}/rates", json={"trade_id": "3316400474147019790", "rate": 1, "feedback": "好买家"}).json()
    assert t["status"] == "success"
    assert len(client.get(f"/api/a/{aid}/rates").json()["rates"]) == n + 1
    assert client.post(f"/api/a/{aid}/rates", json={"trade_id": "1", "rate": 1, "feedback": ""}).status_code == 422


def test_api09_dashboard(client, aid):
    """API-09 仪表盘"""
    d = client.get("/api/dashboard").json()
    assert d["todo"]["wait_consign"] == 6 and d["todo"]["refund_pending"] == 1 and d["todo"]["unread"] == 2
    # 昵称不再取使用者填写的值，而是登录后由 mtop.idle.web.user.page.nav 回填
    assert d["health"][0]["name"] == "闲鱼用户0001" and d["gateway"] == "browser"
    assert d["sales"] and sum(s["orders"] for s in d["sales"]) >= 0


def test_api10_restricted_account(client, aid):
    """API-10 受限账号调用业务接口返回 409 与恢复时间；恢复后正常"""
    c = client.app.state.c
    c.accounts.runtime(aid).transport.inject("mtop.idle.web.xyh.item.list", "RGV587_ERROR::SM::被挤爆啦")
    r = client.get(f"/api/a/{aid}/items")
    assert r.status_code == 502 and r.json()["kind"] == "risk"
    r = client.get(f"/api/a/{aid}/items")
    assert r.status_code == 409 and r.json()["resume_at"] and "手动完成验证" in r.json()["detail"]
    assert client.post(f"/api/accounts/{aid}/resume").json()["status"] == "ok"
    assert client.get(f"/api/a/{aid}/items").status_code == 200


def test_api11_validation(client, aid):
    """API-11 参数校验"""
    oid = client.get(f"/api/a/{aid}/orders/wait-consign").json()["orders"][0]["order_id"]
    assert client.post(f"/api/a/{aid}/orders/{oid}/ship-dummy", json={"text": ""}).status_code == 422
    assert client.post(f"/api/a/{aid}/orders/{oid}/ship-offline", json={"mail_no": "中文单号!!"}).status_code == 422
    assert client.post(f"/api/a/{aid}/items/publish", json={"title": "x", "price": -1, "images": ["u"]}).status_code == 422
    assert client.post(f"/api/a/{aid}/items/batch", json={"action": "price", "item_ids": ["1"], "value": 0}).status_code == 400
    assert client.post("/api/accounts", json={"name": "x" * 65}).status_code == 422
    assert client.post(f"/api/accounts/{aid}/open", params={"url": "https://evil.example/"}).status_code == 400


def test_api12_long_order_id(client, aid):
    """API-12 19 位订单号在 JSON 中为字符串且原样往返"""
    raw = client.get(f"/api/a/{aid}/orders/wait-consign").content.decode()
    oid = client.get(f"/api/a/{aid}/orders/wait-consign").json()["orders"][0]["order_id"]
    assert isinstance(oid, str) and len(oid) == 19 and f'"order_id":"{oid}"' in raw
    assert client.get(f"/api/a/{aid}/orders/{oid}").json()["order_id"] == oid


def test_api13_static(client):
    """API-13 首页与静态资源"""
    html = client.get("/").text
    assert "闲鱼多店铺管理" in html and "/static/brand/logo.png" in html
    assert "演示模式" not in html and 'id="modeTag"' not in html
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/brand/logo.png").status_code == 200


def test_api15_qr_login(client):
    """API-15 应用内扫码：建空账号 → 本地渲染二维码 → 轮询确认 → 昵称头像自动落库"""
    a = client.post("/api/accounts").json()
    assert (a["name"], a["avatar"], a["status"]) == ("", "", "new")

    qr = client.post(f"/api/accounts/{a['id']}/login/qr").json()
    assert qr["qr_data_url"].startswith("data:image/svg+xml;base64,") and qr["token"]
    tok = qr["token"]

    seen = [client.get(f"/api/accounts/{a['id']}/login/qr", params={"token": tok}).json()["status"]
            for _ in range(3)]
    assert seen == ["NEW", "SCANED", "CONFIRMED"]
    last = client.get(f"/api/accounts/{a['id']}/login/qr", params={"token": tok})
    assert last.status_code == 400                       # 会话在确认后已释放
    acc = next(x for x in client.get("/api/accounts").json() if x["id"] == a["id"])
    # 头像必须是前端能直接渲染的绝对地址（Mock 用 data URI，真实接口是 https 的 img.alicdn.com）
    assert acc["status"] == "ok" and acc["name"].startswith("闲鱼用户") and acc["avatar"].startswith(("https://", "data:image/"))
    assert acc["xy_user_id"].startswith("2200")
    assert client.app.state.c.accounts.runtime(a["id"]).transport.login_page_closed


def test_api16_qr_login_cancel(client):
    """API-16 取消扫码：临时账号连同 profile 一并清理，已有昵称的账号保留"""
    a = client.post("/api/accounts").json()
    tok = client.post(f"/api/accounts/{a['id']}/login/qr").json()["token"]
    client.delete(f"/api/accounts/{a['id']}/login/qr")
    assert client.get("/api/accounts").json() == []
    assert client.get(f"/api/accounts/{a['id']}/login/qr", params={"token": tok}).status_code == 400

    named = client.post("/api/accounts", json={"name": "已有店铺"}).json()
    client.post(f"/api/accounts/{named['id']}/login/qr")
    client.delete(f"/api/accounts/{named['id']}/login/qr")
    assert [x["name"] for x in client.get("/api/accounts").json()] == ["已有店铺"]


def test_api14_settings(tmp_path):
    """API-14 设置读写与范围校验（使用真实默认参数，不走测试加速）；任务与审计查询"""
    app = create_app(Settings(data_dir=tmp_path, open_browser=False, workers=False))
    with TestClient(app) as client:
        s = client.get("/api/settings").json()["tunables"]
        assert (s["qps"], s["backoff_base"], s["backoff_cap"], s["max_attempts"], s["risk_pause_min"]) == (0.3, 1.5, 60, 3, 10)
        s["autoreply_cooldown_s"] = 120
        assert client.put("/api/settings", json=s).json()["tunables"]["autoreply_cooldown_s"] == 120
        assert client.get("/api/settings").json()["tunables"]["autoreply_cooldown_s"] == 120
        s["qps"] = 1.0
        assert client.put("/api/settings", json=s).status_code == 422
        assert isinstance(client.get("/api/tasks").json(), list)
        assert any(a["action"] == "settings.save" for a in client.get("/api/audit").json())


def test_autoreply_rules_api(client, aid):
    """D7 规则管理 + 手动扫描"""
    assert client.post("/api/autoreply/rules", json={"keyword": "(", "match_type": "regex", "reply": "x"}).status_code == 400
    r = client.post("/api/autoreply/rules", json={"keyword": "还在", "reply": "在的"}).json()
    assert client.post(f"/api/a/{aid}/autoreply/scan").json()["queued"] == 2
    assert client.post(f"/api/a/{aid}/autoreply/scan").json()["queued"] == 0   # 冷却
    client.delete(f"/api/autoreply/rules/{r['id']}")
    assert client.get("/api/autoreply/rules").json() == []


def test_dead_task_retry_api(client, aid):
    """S1 死信重试接口"""
    c = client.app.state.c
    c.accounts.runtime(aid).transport.inject("mtop.taobao.idle.item.polish", "FAIL_SYS_SERVICE_TIMEOUT::超时", times=3)
    iid = client.get(f"/api/a/{aid}/items").json()["items"][0]["item_id"]
    t = client.post(f"/api/a/{aid}/items/{iid}/action", json={"action": "polish"}).json()
    assert t["status"] == "dead" and t["attempts"] == 3
    assert client.post(f"/api/tasks/{t['id']}/retry").json()["status"] == "pending"
    end = time.time() + 5
    while client.get(f"/api/tasks/{t['id']}").json()["status"] != "success":
        assert time.time() < end
        time.sleep(0.05)


def test_api17_item_cover(client, aid):
    """API-17 商品列表带封面图，且为可直接渲染的绝对地址"""
    rows = client.get(f"/api/a/{aid}/items").json()["items"]
    assert rows
    for r in rows:
        assert r["pic_url"], f"商品 {r['item_id']} 缺少封面图"
        assert r["pic_url"].startswith(("https://", "data:image/"))


def test_api18_delete_keeps_device(client, aid):
    """API-18 删除账号默认保留浏览器 profile（设备身份），wipe=1 才连设备一起清除"""
    svc = client.app.state.c.accounts
    profile = svc.profile_dir(aid)
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "device-marker").write_text("cna", encoding="utf-8")

    client.delete(f"/api/accounts/{aid}")
    assert client.get("/api/accounts").json() == []
    assert (profile / "device-marker").exists(), "删账号不该把设备身份一起删掉"

    # 明确要求彻底清除时才删
    again = client.post("/api/accounts", json={"name": "重建"}).json()
    wiped = svc.profile_dir(again["id"])
    wiped.mkdir(parents=True, exist_ok=True)
    client.delete(f"/api/accounts/{again['id']}", params={"wipe": 1})
    assert not wiped.exists()


def test_api19_qr_login_cooldown(tmp_path):
    """API-19 登录成功后短时间重复扫码被拒（反复扫码会触发风控，登录态反而更短命）"""
    app = create_app(Settings(data_dir=tmp_path, open_browser=False, workers=False),
                     transport_factory=lambda acc: MockTransport(user_id=str(2200000000 + acc.id), seed=acc.id),
                     tunables_override=fast_tunables(qr_login_cooldown_s=600))
    with TestClient(app) as c:
        a = c.post("/api/accounts").json()
        tok = c.post(f"/api/accounts/{a['id']}/login/qr").json()["token"]
        for _ in range(3):
            c.get(f"/api/accounts/{a['id']}/login/qr", params={"token": tok})
        assert next(x for x in c.get("/api/accounts").json() if x["id"] == a["id"])["status"] == "ok"

        r = c.post(f"/api/accounts/{a['id']}/login/qr")
        assert r.status_code == 400 and "秒后再试" in r.json()["detail"]


def test_api20_session_lost_records_ttl(client, aid):
    """API-20 登录态失效时审计里记下这次登录活了多少（判断风控强度用）"""
    rt = client.app.state.c.accounts.runtime(aid)
    client.app.state.c.accounts.audit(aid, "account.login", "扫码登录成功：测试店")
    rt.apply_kind(RetKind.SESSION, "FAIL_SYS_SESSION_EXPIRED::Session过期")
    logs = [x for x in client.get("/api/audit", params={"limit": 20}).json()
            if x["action"] == "account.session_lost"]
    assert logs and "存活" in logs[0]["summary"]


def test_api21_profile_fail_keeps_status(client, aid):
    """API-21 资料接口失败不能把账号判成掉登录态（实测两个接口的判定严格度不一致）"""
    rt = client.app.state.c.accounts.runtime(aid)
    rt.transport.profile_fails = 5          # 让 user.page.nav 持续返回业务失败
    r = client.post(f"/api/accounts/{aid}/check").json()
    assert r["ok"] is True               # loginuser.get 成功就说明登录态没问题
    acc = next(x for x in client.get("/api/accounts").json() if x["id"] == aid)
    assert acc["status"] == "ok", "资料接口失败不该把账号判成掉登录态"
    assert acc["name"], "昵称应保留上一次取到的值"


def test_api22_conv_avatar_and_read(client, aid):
    """API-22 会话列表的对方头像与已读状态（头像后台补，首屏不等）"""
    rows = client.get(f"/api/a/{aid}/im/conversations").json()["conversations"]
    assert rows
    rt = client.app.state.c.accounts.runtime(aid)

    # 首屏不能等资料接口：那个接口单个就可能 20 秒超时，同步补会让页面卡 100 秒。
    # 所以第一次响应允许头像为空，由前端用昵称首字占位，随后 /im/peers 补。
    first = [c for c in rows if c["peer_id"] != "0"]
    assert any(not c["peer_avatar"] for c in first), "首屏不应同步补头像"

    # 后台任务补完后（Mock 很快），下一次请求就该拿到头像了
    deadline = time.time() + 8
    while time.time() < deadline:
        rows = client.get(f"/api/a/{aid}/im/conversations").json()["conversations"]
        if all(c["peer_avatar"] for c in rows if c["peer_id"] != "0"):
            break
        time.sleep(0.15)

    for c in rows:
        if c["peer_id"] == "0":
            continue
        assert c["peer_avatar"].startswith(("https://", "data:image/")), f"{c['peer_id']} 缺头像"
        # 昵称必须是对方的，不能是自己 —— page.nav 带 userId 只会返回登录用户自己（D-20）
        assert c["peer_nick"] and c["peer_nick"] != rt.transport.nick, f"{c['peer_id']} 昵称取成了自己"
    assert any(c["read_status"] == 1 for c in rows), "自己发的最后一条应有已读状态"

    # 缓存热了以后不再打资料接口
    n = len([c for c in rt.transport.calls if c[0] == "mtop.idle.web.user.page.head"])
    client.get(f"/api/a/{aid}/im/conversations")
    assert len([c for c in rt.transport.calls if c[0] == "mtop.idle.web.user.page.head"]) == n


def test_api23_message_card(client, aid):
    """API-23 卡片消息带结构化信息（标题/图片/价格），前端才能渲染成卡片"""
    cards = []
    for c in client.get(f"/api/a/{aid}/im/conversations").json()["conversations"]:
        msgs = client.get(f"/api/a/{aid}/im/messages?cid={c['cid']}").json()["messages"]
        cards += [m for m in msgs if m["kind"] == "card"]
    assert cards, "Mock 里应有一条卡片消息"
    card = cards[0]["card"]
    assert card["title"] and card["price"] and card["pic"].startswith("data:image/")


def test_api24_tip_and_notice_kinds(client, aid):
    """API-24 系统提示与通知卡要能区分开：提示居中成灰字，通知卡才渲染成卡片

    真实账号里"你已确认收货，交易成功"是 contentType=14 的 tip，之前被当成卡片塞进
    黄色气泡，看起来像对方说的话；"快给ta一个评价吧～"是 contentType=25 的通知卡。
    """
    tips, notices, cards = [], [], []
    for c in client.get(f"/api/a/{aid}/im/conversations").json()["conversations"]:
        for m in client.get(f"/api/a/{aid}/im/messages?cid={c['cid']}").json()["messages"]:
            if m["kind"] == "tip":
                tips.append(m)
            elif m["kind"] == "card":
                cards.append(m)
                if (m["card"] or {}).get("action"):
                    notices.append(m)
    assert tips, "系统提示应解析成 kind=tip"
    assert tips[0]["card"] is None, "提示不是卡片，不能带 card 结构"
    assert "确认收货" in tips[0]["text"]
    assert notices, "通知卡应带按钮文案"
    assert notices[0]["card"]["sub"], "通知卡应带副标题"
    assert cards[0]["card"]["ct"] == 7, "商品卡应保留 contentType 供前端分派布局"


def test_api25_read_status_normalized(client, aid):
    """API-25 已读状态归一化：真实接口只给 0/2，前端需要 0/1，且必须带发送者

    实测 30 个会话：readStatus=0 全部 unreadCount=1（未读）、=2 全部 unreadCount=0（已读）。
    发送者在 lastMessage.message.extension.senderUserId，取错路径会让已读状态整列丢失。
    """
    rows = client.get(f"/api/a/{aid}/im/conversations").json()["conversations"]
    assert rows
    for c in rows:
        assert c["read_status"] in (0, 1), f"未归一化：{c['read_status']}"
        assert c["last_sender_id"], "缺 last_sender_id，前端无法判断是不是自己发的"
    # 自己发的最后一条 → 对方已读（Mock 里 readStatus 给的是真实取值 2）
    mine = [c for c in rows if c["last_sender_id"] and c["last_sender_id"] != c["peer_id"]]
    assert mine and all(c["read_status"] == 1 for c in mine)


def test_api26_wait_consign_empty_not_error(client, aid):
    """API-26 没有待发货订单时不能报错

    真实接口（D-24）在"没有待发货"时返回 QUERY_SELLER_TRADE_ORDER_LIST_FAILED，
    以前直接抛出去 → 前端整页红条。现在要当空列表处理。
    """
    orders = client.get(f"/api/a/{aid}/orders/wait-consign").json()["orders"]
    for o in orders:
        client.post(f"/api/a/{aid}/orders/{o['order_id']}/ship-dummy", json={"text": "批量交付"})
    r = client.get(f"/api/a/{aid}/orders/wait-consign")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0
    assert r.json()["orders"] == []


def test_api27_wait_consign_im_fallback(client, aid):
    """API-27 接口说"没有"时，用会话里的订单兜底

    PC 端卖家订单列表接口不可用，靠 order.detail 从会话反查：
    只要订单有聊天记录、且我是卖家、状态是待发货，就能列出来。
    """
    rt = client.app.state.c.accounts.runtime(aid)
    rt.transport.inject("mtop.taobao.idle.logistic.wait.consign.order.query",
                        "QUERY_SELLER_TRADE_ORDER_LIST_FAILED::查询卖家订单列表失败", times=1)
    d = client.get(f"/api/a/{aid}/orders/wait-consign").json()
    assert d["orders"], "兜底应能从会话订单里找回待发货"
    first = d["orders"][0]
    assert first["order_id"] and first["item_title"] and first["buyer_nick"]
    # 兜底拿到的必须是"我作为卖家"的订单
    assert rt.transport.calls and any(
        c[0] == "mtop.idle.web.trade.order.detail" for c in rt.transport.calls)


def test_api28_wait_consign_sends_required_params(client, aid):
    """API-28 待发货接口必须带 accessCode + terminalSource

    真实接口是链式校验：缺 accessCode 报 accessCode，补上才报 terminalSource（D-24）。
    """
    rt = client.app.state.c.accounts.runtime(aid)
    rt.transport.calls.clear()
    client.get(f"/api/a/{aid}/orders/wait-consign")
    calls = [c for c in rt.transport.calls
             if c[0] == "mtop.taobao.idle.logistic.wait.consign.order.query"]
    assert calls, "应调用了待发货接口"
    params = calls[0][1] or {}
    assert params.get("accessCode"), f"缺 accessCode: {params}"
    assert params.get("terminalSource"), f"缺 terminalSource: {params}"
