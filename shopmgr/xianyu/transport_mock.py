"""Mock 传输：内存里的一个虚拟店铺，返回结构与实测样本（xianyu-mtop/data/live）一致。

用途：自动化测试。数据全部为合成数据，生产入口不会加载。
- `inject(api, ret, times=1)`：让接下来 N 次该接口返回指定 ret，用于测试风控/失效/重试。
- `calls`：记录每次调用 (api, data)，便于断言。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import defaultdict

from . import im

OK = ["SUCCESS::调用成功"]


def _resp(api: str, data: dict, v: str = "1.0", ret: list | None = None) -> dict:
    return {"api": api, "v": v, "ret": ret or OK, "data": data}


class MockTransport:
    def __init__(self, user_id: str = "2200000001", seed: int = 0, latency: float = 0.0):
        self.user_id = user_id
        self.latency = latency
        self.calls: list[tuple[str, dict]] = []
        self.opened: list[str] = []
        self.logged_in = True
        # 资料接口返回的昵称/头像（真实接口：mtop.idle.web.user.page.nav）
        self.nick = f"闲鱼用户{user_id[-4:]}"
        # 头像用自包含 data URI：Mock 不依赖外网，且不会出现"1x1 空白图"这种既没报错也没画面的情况
        self.avatar = self._mock_avatar(user_id[-4:])
        self.profile_fails = 0          # >0 时资料接口返回业务失败，用于测试兜底
        self.qr_states = ["NEW", "SCANED", "CONFIRMED"]
        self.qr_generated = 0
        self.login_page_closed = False
        self._inject: dict[str, list[list[str]]] = defaultdict(list)
        self._seq = 1000 + seed * 100
        self._build(seed)

    # ------------------------------------------------------------ 数据
    def _id(self) -> int:
        self._seq += 1
        return self._seq

    def _build(self, seed: int) -> None:
        base_item, base_order = 1093170000000 + seed * 1000, 4502035009004022000 + seed * 1000
        titles = ["九成新 Switch 游戏卡带", "办公软件安装远程指导", "二手机械键盘 青轴", "Python 入门答疑 1 小时",
                  "全新未拆 蓝牙耳机", "旧书 · 算法导论"]
        self.items = {
            str(base_item + i): {"title": t, "price": f"{(i + 1) * 12.5:.2f}", "status": 0 if i < 5 else 2,
                                 "desc": f"{t}，诚心出。", "quantity": 1, "group": "在售" if i < 5 else "已下架"}
            for i, t in enumerate(titles)
        }
        self.drafts: dict[str, dict] = {}
        ids = list(self.items)
        # 待发货：19 位订单号，刻意超过 2^53
        self.wait_orders = {
            str(base_order + i): {"item_id": ids[i % 4], "buyer": f"买家{i + 1:02d}", "value": (i + 1) * 990,
                                  "pay": f"2026-10-2{i % 9} 1{i}:20:00"}
            for i in range(6)
        }
        self.shipped: dict[str, dict] = {}
        self.closed: set[str] = set()
        self.refunds = {
            str(290891714435569000 + i): {"order_id": str(3316978525130041000 + i), "amount": f"{20 + i * 5:.2f}",
                                          "status": 1 if i == 0 else 5, "reason": "协商一致退款", "title": titles[i]}
            for i in range(3)
        }
        self.blacklist: set[str] = set()
        self.rates = [
            {"rateId": 8000 + i, "rate": 1, "feedback": f"很好的卖家，第 {i + 1} 次购买", "raterUserNick": f"买家{i + 1:02d}",
             "gmtCreate": f"2026-10-2{i} 12:00:00", "rateTagList": [{"text": "发货快"}]}
            for i in range(4)
        ]
        self.convs: dict[str, dict] = {}
        for i in range(3):
            cid, peer = str(68032467000 + i), str(3395440000 + i)
            # 会话带上 orderId：待发货兜底聚合要靠它从会话反查订单详情
            self.convs[cid] = {"peer": peer, "nick": f"买家{i + 1:02d}", "item_id": ids[i],
                               "order_id": list(self.wait_orders)[i],
                               "unread": 1 if i < 2 else 0, "messages": []}
            now = 1793255000000 + i * 1000
            self._add_msg(cid, peer, "你好，还在吗？", now)
            if i == 2:
                # 三类非文本消息各来一条，覆盖卡片 / 系统提示 / 通知卡的渲染
                self._add_msg(cid, self.user_id, "[我已发货] 点击查看订单", now + 250,
                              card={"item_id": ids[2], "title": self.items[ids[2]]["title"],
                                    "price": "12.50", "pic": self._mock_pic(1), "tip": "我想要"})
                self._add_msg(cid, self.user_id, "你已确认收货，交易成功", now + 380, kind="tip")
                self._add_msg(cid, self.user_id, "", now + 420, kind="notice",
                              card={"title": "快给ta一个评价吧～",
                                    "sub": "交易体验还满意吗？评价帮更多人选购", "action": "去评价"})
                self._add_msg(cid, self.user_id, "在的，欢迎下单", now + 500)

    def _add_msg(self, cid: str, sender: str, text: str, ts: int | None = None,
                 card: dict | None = None, kind: str = "text") -> dict:
        """构造一条消息。结构按线上实际响应构造，让解析层走的是真实路径而不是理想结构。"""
        ts = ts or int(time.time() * 1000)
        card = card or {}
        if kind == "tip":
            # contentType=14 系统提示：{"tip": {"tip": "你已确认收货，交易成功"}}
            payload = {"contentType": 14, "tip": {"tip": text}}
            summary, ctype = f"[{text}]", 14
        elif kind == "notice":
            # contentType=25 平台通知卡：标题/副标题/按钮都在 dxCard.item.main.exContent
            payload = {"contentType": 25, "dxCard": {"item": {"main": {"exContent": {
                "title": card.get("title", ""), "subTitle": card.get("sub", ""),
                "button": {"text": card.get("action", "")}}}}}}
            summary, ctype = card.get("title", text), 25
        elif card:
            # contentType=7 商品卡：业务字段在 itemCard.item（mainPic 不是 itemMainPic）
            payload = {"contentType": 7, "itemCard": {"item": {
                "itemId": card.get("item_id", ""), "title": card.get("title", ""),
                "mainPic": card.get("pic", ""), "price": card.get("price", "")},
                "itemTip": card.get("tip", "")}}
            summary, ctype = text, 7
        else:
            payload = {"contentType": 1, "text": {"text": text}}
            summary, ctype = text, 1
        data = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        content = {"contentType": 101, "custom": {"type": ctype, "summary": summary, "data": data}}
        msg = {"messageId": f"{self._id()}.PNM", "cid": f"{cid}@goofish", "createAt": ts,
               "sender": {"uid": f"{sender}@goofish"},
               "extension": {"senderUserId": sender, "reminderTitle": self.convs[cid]["nick"],
                             "reminderContent": summary},
               "content": content}
        self.convs[cid]["messages"].append(msg)
        return msg

    def buyer_says(self, cid: str, text: str) -> None:
        """测试辅助：模拟买家发来消息。"""
        self._add_msg(cid, self.convs[cid]["peer"], text)
        self.convs[cid]["unread"] += 1

    # ------------------------------------------------------------ 控制
    def inject(self, api: str, ret: str, times: int = 1) -> None:
        self._inject[api].extend([[ret]] * times)

    def count(self, api: str) -> int:
        return sum(1 for a, _ in self.calls if a == api)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------ 扫码登录（A1）
    async def start_qr_login(self) -> dict:
        """返回与 passport `generate.do` 同构的字段；codeContent 由上层渲染成二维码。"""
        self.qr_generated += 1
        self.login_page_closed = False
        return {"t": 1700000000000 + self.qr_generated, "ck": f"mockck{self.qr_generated}",
                "code_content": f"https://passport.goofish.com/qr/{self.user_id}/{self.qr_generated}"}

    async def poll_qr_login(self, t: int, ck: str) -> dict:
        status = self.qr_states.pop(0) if len(self.qr_states) > 1 else self.qr_states[0]
        return {"status": status, "result_code": 10000 if status == "CONFIRMED" else 0, "title": ""}

    async def close_login_page(self) -> None:
        self.login_page_closed = True

    async def reload_home(self) -> None:
        return None

    # ------------------------------------------------------------ mtop
    async def mtop(self, api: str, v: str, data: dict) -> dict:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.calls.append((api, data))
        if self._inject.get(api):
            return _resp(api, {}, v, self._inject[api].pop(0))
        if not self.logged_in:
            return _resp(api, {}, v, ["FAIL_SYS_SESSION_EXPIRED::Session过期"])
        handler = getattr(self, "_h_" + api.replace(".", "_"), None)
        if handler is None:
            return _resp(api, {"mock": True, "echo": data}, v)
        out = handler(data)
        if isinstance(out, tuple):
            return _resp(api, {}, v, [out[1]])
        return _resp(api, out, v)

    # ---- 账号
    def _h_mtop_taobao_idlemessage_pc_loginuser_get(self, d):
        return {"userId": int(self.user_id), "nick": self.nick}

    def _h_mtop_idle_web_user_page_nav(self, d):
        """查自己：module.base.displayName / avatar（字符串）。

        真实接口即使带 userId 也只返回登录用户自己（见 D-20），这里照抄这个坑，
        免得测试把一个用错的接口当成能查别人。
        """
        if self.profile_fails > 0:
            self.profile_fails -= 1
            return None, "FAIL_BIZ_PROFILE_EMPTY::未返回用户昵称"
        return {"module": {"base": {"displayName": self.nick, "displayNick": self.nick,
                                    "avatar": self.avatar}}}

    def _h_mtop_idle_web_user_page_head(self, d):
        """查别人：module.base.displayName + avatar 嵌套对象 {avatar, avatarPendant}。

        userId 必填，不带参数真实接口报 FAIL_BIZ_CLIENT_PARAM_INVALID。
        """
        uid = str((d or {}).get("userId") or "")
        if not uid:
            return None, "FAIL_BIZ_CLIENT_PARAM_INVALID::不合法的参数，请检查"
        if self.profile_fails > 0:
            self.profile_fails -= 1
            return None, "FAIL_BIZ_PROFILE_EMPTY::未返回用户昵称"
        tail = uid[-2:]
        return {"module": {"base": {
            "displayName": f"买家{tail}", "ipLocation": "浙江省",
            "avatar": {"avatar": self._mock_avatar(tail),
                       "avatarPendant": ""}}}}

    def _h_mtop_taobao_idlemessage_pc_redpoint_query(self, d):
        return {"total": sum(c["unread"] for c in self.convs.values())}

    # ---- 商品
    @staticmethod
    def _mock_avatar(tail: str) -> str:
        """自包含的头像占位（data URI）：深色圆底 + 人形剪影，不依赖外网。"""
        colors = ["4b6bfb", "f2733f", "00a870", "7b5cf0", "d4380d", "0b7fd4"]
        c = colors[sum(map(ord, tail)) % len(colors)]
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='80' height='80'>"
               f"<rect width='80' height='80' fill='%23{c}'/>"
               f"<circle cx='40' cy='31' r='13' fill='%23ffffff'/>"
               f"<path d='M12 74c0-15 12.5-23 28-23s28 8 28 23z' fill='%23ffffff'/></svg>")
        return "data:image/svg+xml;utf8," + svg

    @staticmethod
    def _mock_pic(i: int) -> str:
        """自包含的占位封面（data URI），让 Mock 界面的缩略图真的能显示出来。"""
        colors = ["ffd21e", "ffe08a", "ffc9c9", "c9e7ff", "d3f4d3", "e6d7ff"]
        c = colors[i % len(colors)]
        # 画成"风景照"的意象（太阳 + 山），一眼能看出这是商品封面占位，不会和头像混淆
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'>"
               f"<rect width='96' height='96' fill='%23{c}'/>"
               f"<circle cx='68' cy='28' r='9' fill='%23ffffff' opacity='.9'/>"
               f"<path d='M10 80 L36 44 L56 70 L68 56 L88 80 Z' fill='%23ffffff' opacity='.9'/></svg>")
        return "data:image/svg+xml;utf8," + svg

    def _card(self, iid, it):
        return {"cardType": 1003, "cardData": {
            "id": iid, "title": it["title"], "itemStatus": it["status"], "categoryId": 50023914,
            "priceInfo": {"preText": "¥", "price": it["price"]},
            "picInfo": {"picUrl": it.get("pic") or self._mock_pic(sum(map(ord, str(iid))) % 6),
                        "width": 800, "height": 800}}}

    def _h_mtop_idle_web_xyh_item_list(self, d):
        rows = [(k, v) for k, v in self.items.items() if not d.get("groupName") or d["groupName"] in ("综合", v["group"])]
        size, page = int(d.get("pageSize") or 20), int(d.get("pageNumber") or 1)
        part = rows[(page - 1) * size: page * size]
        groups = [{"groupId": 1, "groupName": "综合", "itemNumber": len(self.items)},
                  {"groupId": 2, "groupName": "在售", "itemNumber": sum(v["status"] == 0 for v in self.items.values())},
                  {"groupId": 3, "groupName": "已下架", "itemNumber": sum(v["status"] == 2 for v in self.items.values())}]
        return {"cardList": [self._card(k, v) for k, v in part], "itemGroupList": groups,
                "totalCount": len(rows), "nextPage": page * size < len(rows)}

    def _h_mtop_taobao_idle_pc_detail(self, d):
        it = self.items.get(str(d.get("itemId")))
        return ({"itemDO": {"itemId": d["itemId"], **it}} if it else (None, "FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"))

    def _h_mtop_taobao_idleitem_badwords_prepubcheck(self, d):
        hits = [w for w in ("微信", "加V", "刷单") if w in (d.get("title", "") + d.get("desc", ""))]
        return {"hitWords": hits}

    def _h_mtop_taobao_idle_kgraph_property_recommend(self, d):
        return {"properties": [{"propertyName": "成色", "valueName": "九成新"},
                               {"propertyName": "交易方式", "valueName": "邮寄"}]}

    def _save_item(self, d, iid=None):
        iid = iid or str(1093179000000 + self._id())
        self.items[iid] = {"title": d.get("title"), "price": f"{int(d.get('price') or 0) / 100:.2f}",
                           "status": 0, "desc": d.get("desc", ""), "quantity": int(d.get("quantity") or 1), "group": "在售"}
        return iid

    def _h_mtop_idle_pc_idleitem_publish(self, d):
        if not d.get("uniqueCode"):
            return None, "FAIL_BIZ_PARAM::缺少 uniqueCode"
        if not d.get("imageInfos"):
            return None, "FAIL_BIZ_NO_IMAGE::请至少上传一张图片"
        for iid, it in self.items.items():   # uniqueCode 幂等
            if it.get("unique") == d["uniqueCode"]:
                return {"itemId": iid}
        iid = self._save_item(d)
        self.items[iid]["unique"] = d["uniqueCode"]
        return {"itemId": iid}

    def _h_mtop_idle_pc_idleitem_editDetail(self, d):
        it = self.items.get(str(d.get("itemId")))
        if not it:
            return None, "FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"
        return {"title": it["title"], "desc": it["desc"], "price": it["price"], "quantity": it["quantity"],
                "imageInfos": [{"url": "https://img.example/mock.jpg"}]}

    def _h_mtop_idle_pc_idleitem_edit(self, d):
        iid = str(d.get("itemId"))
        if iid not in self.items:
            return None, "FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"
        status, group = self.items[iid]["status"], self.items[iid]["group"]
        self._save_item(d, iid)
        self.items[iid].update(status=status, group=group)
        return {"itemId": iid}

    def _set_status(self, d, status, group):
        iid = str(d.get("itemId"))
        if iid not in self.items:
            return None, "FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"
        self.items[iid].update(status=status, group=group)
        return {"result": True}

    def _h_mtop_idle_idleitem_item_open(self, d):
        return self._set_status(d, 0, "在售")

    def _h_mtop_idle_idleitem_item_cancel(self, d):
        return self._set_status(d, 2, "已下架")

    def _h_mtop_taobao_idle_item_polish(self, d):
        it = self.items.get(str(d.get("itemId")))
        if not it:
            return None, "FAIL_BIZ_ITEM_NOT_EXIST::商品不存在"
        if it["status"] != 0:
            return None, "FAIL_BIZ_ITEM_STATUS::仅在售商品可擦亮"
        return {"result": True}

    def _h_mtop_idle_idleitem_draft_edit(self, d):
        did = str(d.get("draftId") or self._id())
        self.drafts[did] = {"draftId": did, "title": d.get("title"), "gmtModified": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "payload": d}
        return {"draftId": did}

    def _h_mtop_idle_idleitem_draft_list(self, d):
        return {"list": [{k: v for k, v in x.items() if k != "payload"} for x in self.drafts.values()]}

    def _h_mtop_idle_idleitem_draft_publish(self, d):
        dr = self.drafts.pop(str(d.get("draftId")), None)
        if not dr:
            return None, "FAIL_BIZ_DRAFT_NOT_EXIST::草稿不存在"
        return {"itemId": self._save_item(dr["payload"])}

    def _h_mtop_idle_idleitem_draft_delete(self, d):
        self.drafts.pop(str(d.get("draftId")), None)
        return {"result": True}

    # ---- 订单
    def _h_mtop_taobao_idle_logistic_wait_consign_order_query(self, d):
        # 照抄真实接口的链式校验：缺 accessCode 先报 accessCode，补上才报 terminalSource（D-24）
        d = d or {}
        if not d.get("accessCode"):
            return None, "FAIL_SYS_BIZPARAM_MISSED::缺少业务参数accessCode"
        if not d.get("terminalSource"):
            return None, "FAIL_SYS_BIZPARAM_MISSED::缺少业务参数terminalSource"
        # 没有待发货订单时，真实接口返回"查询失败"而不是空列表
        if not self.wait_orders:
            return None, "QUERY_SELLER_TRADE_ORDER_LIST_FAILED::查询卖家订单列表失败"
        infos = [{"groupType": "normal", "packageId": oid, "tradeIdList": [oid], "bizOrderInfoList": [{
            "bizOrderId": oid, "auctionId": o["item_id"], "auctionTitle": self.items[o["item_id"]]["title"],
            "auctionPictUrl": "", "buyerNick": o["buyer"], "orderValue": o["value"], "buyAmount": 1,
            "payTime": o["pay"], "gmtCreate": o["pay"]}]} for oid, o in self.wait_orders.items()]
        return {"data": {"batchConsignSummaryInfo": {"subOrderCnt": len(infos)}, "consignOrderInfos": infos}}

    def _ship(self, oid, how):
        if oid not in self.wait_orders:
            return None, "FAIL_BIZ_ORDER_STATUS::订单状态不允许发货"
        self.shipped[oid] = {**self.wait_orders.pop(oid), "how": how}
        return {"data": {"orderId": self._id() + 4600000000000000000}}

    def _h_mtop_taobao_idle_logistic_consign_dummy(self, d):
        if not d.get("tradeText"):
            return None, "FAIL_BIZ_PARAM::交付说明不能为空"
        return self._ship(str(d.get("orderId")), "dummy")

    def _h_mtop_taobao_idle_logistic_consign_offline(self, d):
        if not d.get("mailNo") or not d.get("cpCode"):
            return None, "FAIL_BIZ_PARAM::运单号或快递公司缺失"
        return self._ship(str(d.get("orderId")), "offline")

    def _h_mtop_cainiao_tdtradebusiness_guess_mailno(self, d):
        no = str(d.get("mailNo") or "")
        table = {"SF": ("SF", "顺丰速运"), "YT": ("YTO", "圆通速递"), "JD": ("JD", "京东快递")}
        cp, name = table.get(no[:2].upper(), ("ZTO", "中通快递"))
        return {"result": [{"cpCode": cp, "cpName": name, "brandCode": cp}]}

    def _h_mtop_idle_web_trade_bought_list(self, d):
        rows = []
        for oid, o in list(self.shipped.items()) + list(self.wait_orders.items()):
            rows.append({"commonData": {"orderId": int(oid), "orderIdStr": oid, "itemId": int(o["item_id"]), "seller": True,
                                        "tradeStatusEnum": "SHIPPED" if oid in self.shipped else "PAID"},
                         "head": {"data": {"createTime": o["pay"], "statusViewMsg": "已发货" if oid in self.shipped else "待发货",
                                           "userInfo": {"userNick": o["buyer"]}}},
                         "content": {"data": {"detailInfo": {"auctionTitle": self.items[o["item_id"]]["title"]},
                                              "priceInfo": {"price": f"{o['value'] / 100:.2f}"}}}})
        return {"items": rows, "totalCount": len(rows), "nextPage": False}

    def _h_mtop_idle_web_trade_order_detail(self, d):
        oid = str(d.get("tid"))
        o = self.wait_orders.get(oid) or self.shipped.get(oid)
        if not o:
            return None, "BIZ_ORDER_NOT_FOUND::订单不存在"
        shipped = oid in self.shipped
        title = "交易关闭" if oid in self.closed else ("卖家已发货" if shipped else "买家已付款，等待发货")
        return {"orderId": oid, "itemId": o["item_id"], "status": "SHIPPED" if shipped else "PAID",
                "peerUserId": "3395440000", "seller": True,
                # 真实结构里状态文案有两个位置：utArgs.orderMainTitle 与 components 里的 orderStatusInfo
                "utArgs": {"orderMainTitle": title},
                "commonInfo": {"createTime": o["pay"]},
                "components": [
                    {"render": "orderStatusVO", "data": {"orderStatusInfo": {"title": title}}},
                    {"render": "itemVO", "data": {"itemInfo": {
                        "title": self.items[o["item_id"]]["title"], "price": "12.50", "buyAmount": "1",
                        "itemMainPictCdnUrl": self._mock_pic(2)}}},
                ],
                "bottomBarVO": {"buttonList": [
                    {"name": "去发货", "tradeAction": "CONSIGN", "clickEvent": {"type": "openPage", "data": {"url": "https://h5.m.goofish.com/"}}},
                    {"name": "修改价格", "tradeAction": "MODIFY_PRICE", "clickEvent": {"type": "actionCode", "data": {"code": "MODIFY_PRICE"}}}]}}

    def _h_mtop_taobao_idle_trade_user_adjust_price(self, d):
        oid = str(d.get("bizOrderId"))
        if oid not in self.wait_orders:
            return None, "FAIL_BIZ_ORDER_STATUS::仅待发货订单可改价"
        self.wait_orders[oid]["value"] = int(d.get("price") or 0)
        return {"result": True}

    def _h_mtop_taobao_idle_trade_order_close_reason_get(self, d):
        return {"reasonList": [{"reasonCode": "1", "reasonText": "买家不想要了"},
                               {"reasonCode": "2", "reasonText": "商品已售出"}]}

    def _h_mtop_taobao_idle_trade_close_by_seller(self, d):
        oid = str(d.get("bizOrderId"))
        if oid not in self.wait_orders:
            return None, "FAIL_BIZ_ORDER_STATUS::订单状态不允许关闭"
        self.closed.add(oid)
        self.shipped[oid] = self.wait_orders.pop(oid)
        return {"result": True}

    def _h_mtop_taobao_idle_trade_seller_delay_confirm(self, d):
        return {"result": True} if str(d.get("orderId")) in self.shipped else (None, "FAIL_BIZ_ORDER_STATUS::仅已发货订单可延长")

    # ---- 消息（HTTP 侧）
    def _h_mtop_taobao_idlemessage_quickreply_list_get_v1(self, d):
        return {"list": [{"content": "在的，亲，可以直接拍"}, {"content": "今天下单今天发货"}, {"content": "不议价哦"}]}

    def _h_mtop_taobao_idlemessage_message_card_send(self, d):
        cid = str(d.get("sessionId"))
        if cid not in self.convs:
            return None, "FAIL_BIZ_SESSION::会话不存在"
        self._add_msg(cid, self.user_id, f"[商品卡片] {self.items.get(str(d.get('itemId')), {}).get('title', '')}")
        return {"result": True}

    def _h_mtop_taobao_idlemessage_pc_blacklist_query(self, d):
        return {"inBlacklist": str(d.get("sessionId")) in self.blacklist}

    def _h_mtop_taobao_idlemessage_pc_blacklist_add(self, d):
        self.blacklist.add(str(d.get("sessionId")))
        return {"result": True}

    def _h_mtop_taobao_idlemessage_pc_blacklist_remove(self, d):
        self.blacklist.discard(str(d.get("sessionId")))
        return {"result": True}

    # ---- 售后
    def _h_mtop_taobao_idle_merchant_refund_list(self, d):
        return {"list": [{"refundId": rid, "orderId": r["order_id"], "itemTitle": r["title"], "refundFee": r["amount"],
                          "refundStatus": r["status"], "reason": r["reason"], "gmtCreate": "2026-10-28 09:00:00"}
                         for rid, r in self.refunds.items()], "totalCount": len(self.refunds)}

    def _h_mtop_taobao_idle_refund_detail(self, d):
        rid = str(d.get("refundId"))
        r = self.refunds.get(rid)
        if not r:
            return None, "FAIL_BIZ_REFUND_NOT_EXIST::退款单不存在"
        buttons = []
        if r["status"] == 1:
            buttons = [
                {"code": "agree", "name": "同意退款", "clickEvent": {"type": "api", "data": {
                    "apiName": "mtop.taobao.idle.refund.agree.refund", "apiVersion": "1.0", "params": {"refundId": rid}}}},
                {"code": "refuse", "name": "拒绝退款", "clickEvent": {"type": "api", "data": {
                    "apiName": "mtop.taobao.idle.refund.seller.refuse", "apiVersion": "1.0",
                    "params": {"refundId": rid, "refuseReason": "商品无问题"}}}},
                {"code": "contact", "name": "联系买家", "clickEvent": {"type": "url", "data": {"url": "https://www.goofish.com/im"}}},
            ]
        return {"data": {"refundId": rid, "orderId": r["order_id"], "refundStatus": r["status"], "idleRefundStatus": "010",
                         "itemId": "", "seller": True, "components": [
                             {"render": "nodeStatusInfo", "data": {"nodeStatusList": [{"title": "买家申请"}, {"title": "卖家处理"}, {"title": "退款结束"}]}},
                             {"render": "refundInfo", "data": {"refundFee": r["amount"], "reason": r["reason"]}},
                             {"render": "bottomBar", "data": buttons}]}}

    def _h_mtop_taobao_idle_refund_seller_refuse(self, d):
        r = self.refunds.get(str(d.get("refundId")))
        if not r or r["status"] != 1:
            return None, "FAIL_BIZ_REFUND_STATUS::当前状态不可拒绝"
        r["status"] = 6
        return {"result": True}

    def _h_mtop_taobao_idle_refund_agree_refund(self, d):
        if not d.get("authToken"):
            return None, "FAIL_BIZ_NEED_PAY_PASSWORD::需要支付密码校验"
        return {"result": True}

    def _h_mtop_taobao_idle_dispute_message_for_xiaoer(self, d):
        return {"result": True}

    # ---- 评价
    def _h_mtop_idle_web_trade_rate_list(self, d):
        return {"cardList": [{"cardType": 1, "cardData": r} for r in self.rates], "totalCount": len(self.rates),
                "nextPage": False}

    def _h_mtop_taobao_idle_rate_create(self, d):
        if not isinstance(d.get("rate"), int):
            return None, "FAIL_BIZ_PARAM::请选择好评/中评/差评～"
        if not d.get("feedback"):
            return None, "FAIL_BIZ_PARAM::评价必须填写内容"
        self.rates.insert(0, {"rateId": self._id(), "rate": d["rate"], "feedback": d["feedback"],
                              "raterUserNick": "我", "gmtCreate": time.strftime("%Y-%m-%d %H:%M:%S"), "rateTagList": []})
        return {"ratedUid": "3395440000"}

    # ---- 统计
    def _h_mtop_taobao_idle_shop_exposure(self, d):
        return {"exposure": 1280, "view": 342}

    # ------------------------------------------------------------ LWP
    async def lwp(self, route: str, body: list) -> dict:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.calls.append((route, {"body": body}))
        if self._inject.get(route):
            ret = self._inject[route].pop(0)[0]
            return {"code": 500, "err": ret, "body": {}}
        if not self.logged_in:
            return {"code": 401, "err": "FAIL_SYS_SESSION_EXPIRED::Session过期", "body": {}}
        headers = {"mid": im.make_mid()}
        if route == im.ROUTE_CONV_LIST:
            convs = sorted(self.convs.items(), key=lambda kv: -kv[1]["messages"][-1]["createAt"])
            out = []
            for cid, c in convs[: int(body[1])]:
                last = c["messages"][-1]
                mine = last["sender"]["uid"].startswith(self.user_id)
                # 真实接口的 readStatus 只有 0 和 2（实测 30 个会话：0 全部未读、2 全部已读），
                # 这里照抄 2 而不是 1，用来验证 client 侧把 >=1 归一成"已读"。
                out.append({"type": 1, "singleChatUserConversation": {
                    "redPoint": c["unread"], "modifyTime": last["createAt"],
                    "lastMessage": {
                        "message": {**last, "unreadCount": 1 if c["unread"] else 0,
                                    "msgReadStatusSetting": 1},
                        "readStatus": 2 if mine else 0},
                    "singleChatConversation": {
                        "cid": f"{cid}@goofish", "pairFirst": f"{c['peer']}@goofish",
                        "pairSecond": f"{self.user_id}@goofish",
                        "extension": {"itemId": c["item_id"],
                                      "itemTitle": self.items[c["item_id"]]["title"],
                                      "orderId": c.get("order_id", "")}}}})
            return {"code": 200, "headers": headers, "body": {"userConvs": out, "nextCursor": 0, "hasMore": 0}}
        if route == im.ROUTE_MSG_LIST:
            cid = im.strip_domain(body[0])
            conv = self.convs.get(cid)
            if not conv:
                return {"code": 500, "err": "500100001::system is error", "body": {}}
            conv["unread"] = 0
            msgs = [m for m in conv["messages"] if m["createAt"] < int(body[2])]
            msgs = sorted(msgs, key=lambda m: -m["createAt"])[: int(body[3])]
            more = len([m for m in conv["messages"] if m["createAt"] < int(body[2])]) > len(msgs)
            return {"code": 200, "headers": headers, "body": {
                "userMessageModels": [{"message": m} for m in msgs],
                "nextCursor": msgs[-1]["createAt"] if msgs else 0, "hasMore": int(more)}}
        if route == im.ROUTE_SEND:
            model, scope = body
            receivers = scope.get("actualReceivers") or []
            custom = model["content"].get("custom") or {}
            if (model["content"].get("contentType") != 101 or custom.get("type") != 1 or "summary" in custom
                    or len(receivers) < 2 or model.get("redPointPolicy") != 0 or not model.get("ctx")):
                return {"code": 500, "err": "500100001::system is error", "body": {}}
            cid = im.strip_domain(model["cid"])
            _, text, _ = im.decode_content(model["content"])
            self._add_msg(cid, self.user_id, text)
            return {"code": 200, "headers": headers, "body": {"uuid": model["uuid"]}}
        return {"code": 404, "err": f"LWP_404::{route}", "body": {}}

    async def upload(self, filename: str, content: bytes) -> dict:
        self.calls.append(("stream-upload", {"filename": filename, "size": len(content)}))
        if len(content) < 1024:
            return {"success": False, "errorCode": "PICTURE_NOT_SAFE", "message": "图片存在安全问题"}
        return {"success": True, "object": {"url": f"https://img.alicdn.com/mock/{self._id()}.jpg"}}

    async def open_page(self, url: str) -> None:
        self.opened.append(url)
