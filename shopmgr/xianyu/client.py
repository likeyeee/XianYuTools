"""闲鱼接口封装：每个平台接口对应一个方法，入参与返回均有类型。

- 所有方法经 `_call()` 发出，非 SUCCESS 统一抛 `MtopError`（带分类）。
- 标注「待联调」的接口入参来自源码静态提取、尚未经真实网关验证，
  方法提供 `extra` 透传字段，联调补参无需改动调用方。
- 解析器对未知结构做宽松处理：取不到的字段给默认值，原始响应可通过 `raw=True` 拿到。
"""
from __future__ import annotations

import asyncio
import json
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from . import im
from .errors import MtopError, RetKind, classify
from .models import (
    ITEM_STATUS, REFUND_STATUS, CloseReason, Conversation, ConversationPage, Draft, Exposure, Item,
    ItemGroup, ItemPage, LoginUser, MailNoGuess, Message, MessagePage, OrderButton, OrderDetail, OrderPage,
    OrderSummary, PropSuggestion, PublishForm, QuickReply, Rate, RateForm, RatePage, Refund, RefundAction,
    RefundDetail, RefundPage, ShipResult, UploadResult, UserProfile, WaitConsignOrder, WaitConsignPage,
)
from .transport import Transport

REFUND_DETAIL_PAGE = ("https://h5.m.goofish.com/wow/moyu/moyu-project/idle-reverse/pages/refundDetail"
                      "?kun=true&orderId={order_id}&refundId={refund_id}")
ORDER_DETAIL_PAGE = "https://www.goofish.com/order-detail?orderId={order_id}"


# ------------------------------------------------------------------ 解析工具
def pick(d: Any, *paths: str, default: Any = "") -> Any:
    """按 'a.b.c' 路径依次尝试取值，返回第一个非空结果。"""
    for path in paths:
        cur = d
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return default


def find_list(obj: Any, key: str) -> list[dict]:
    """在未知结构中找第一个「元素为 dict 且含 key」的列表（广度优先）。"""
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, list):
            if cur and isinstance(cur[0], dict) and any(key in x for x in cur if isinstance(x, dict)):
                return [x for x in cur if isinstance(x, dict)]
            queue.extend(cur)
        elif isinstance(cur, dict):
            queue.extend(cur.values())
    return []


# ------------------------------------------------------- 待发货（C3）辅助
# 这两个 ret 表示"没有待发货订单"，不是真失败 —— 抛出去前端就弹红条。
_EMPTY_CONSIGN_CODES = ("QUERY_SELLER_TRADE_ORDER_LIST_FAILED", "FAIL_BIZ_EMPTY_RESULT")
# 待发货判定用文案而不是数字枚举：status 3=已发货、4=交易成功、12=关闭，
# 待发货的数值没拿到样本，文案更稳。
_WAIT_WORDS = ("待发货", "等待发货", "待卖家发货", "等待卖家发货", "等待你发货")


def _is_empty_consign(err: Exception) -> bool:
    """接口用"查询失败"表达"没有待发货"，别当异常抛。"""
    ret = getattr(err, "ret", None) or []
    return any(any(code in str(r) for code in _EMPTY_CONSIGN_CODES) for r in ret)


def _is_wait_consign(status_text: str) -> bool:
    """文案里带"待发货"才算；"已发货，待买家确认收货"不算。"""
    text = str(status_text or "")
    if "已发货" in text or "已寄出" in text:
        return False
    return any(w in text for w in _WAIT_WORDS)


def _order_status_title(detail: dict) -> str:
    """状态文案：优先 utArgs.orderMainTitle，其次 components 里的 orderStatusInfo.title。"""
    got = pick(detail, "utArgs.orderMainTitle", default="")
    if got:
        return str(got)
    for comp in detail.get("components") or []:
        info = (comp.get("data") or {}).get("orderStatusInfo") if isinstance(comp, dict) else None
        if isinstance(info, dict) and info.get("title"):
            return str(info["title"])
    return ""


def _order_item_info(detail: dict) -> dict:
    """商品信息块：components[].data.itemInfo（标题/主图/单价/数量）。"""
    for comp in detail.get("components") or []:
        data = (comp.get("data") or {}) if isinstance(comp, dict) else {}
        info = data.get("itemInfo")
        if isinstance(info, dict) and (info.get("title") or info.get("itemMainPictCdnUrl")):
            return info
    return {}


def id_str(d: dict, *names: str) -> str:
    """取 ID：优先 *Str 版本，保持字符串，杜绝 19 位订单号精度丢失。"""
    for n in names:
        for k in (n + "Str", n):
            v = d.get(k) if isinstance(d, dict) else None
            if v not in (None, ""):
                return str(v)
    return ""


def fen_to_yuan(v: Any) -> str:
    try:
        return f"{(Decimal(str(v)) / 100):.2f}"
    except (InvalidOperation, ValueError):
        return ""


def yuan(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):.2f}"
    except (InvalidOperation, ValueError):
        return str(v or "")


def img_url(v: Any) -> str:
    """图片地址归一化：补全协议头（平台返回的常见是 `//img.alicdn.com/...`），统一走 https。"""
    u = str(v or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    return u


def first_image(detail_params: dict) -> str:
    """兜底：detailParams.imageInfos 是 JSON 字符串，取 major 的那张作为封面（已归一化）。"""
    raw = detail_params.get("imageInfos") if isinstance(detail_params, dict) else None
    if not raw:
        return ""
    try:
        arr = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return ""
    if not isinstance(arr, list):
        return ""
    rows = [x for x in arr if isinstance(x, dict) and x.get("url")]
    for x in rows:
        if x.get("major") is True:
            return img_url(x["url"])
    return img_url(rows[0]["url"]) if rows else ""


class XianyuClient:
    def __init__(self, transport: Transport):
        self.t = transport
        self.self_id: str = ""

    # ---------------------------------------------------------------- 通用
    async def _call(self, api: str, v: str = "1.0", data: dict | None = None) -> dict:
        res = await self.t.mtop(api, v, data or {})
        ret = res.get("ret") if isinstance(res, dict) else None
        if classify(ret) is not RetKind.OK:
            raise MtopError(api, ret, res if isinstance(res, dict) else None)
        return res.get("data") or {}

    async def _lwp(self, route: str, body: list) -> dict:
        res = await self.t.lwp(route, body)
        code = res.get("code")
        if code != 200:
            err = res.get("err") or f"LWP_{code}::{json.dumps(res.get('body'), ensure_ascii=False)[:200]}"
            raise MtopError(route, [err if "::" in err else f"{err}::{err}"], res)
        return res.get("body") or {}

    async def call_raw(self, api: str, v: str = "1.0", data: dict | None = None) -> dict:
        """通用调用（售后动态按钮 E4、排查用）。"""
        return await self._call(api, v, data)

    # ---------------------------------------------------------------- A 账号
    async def login_user(self) -> LoginUser:
        """A2 mtop.taobao.idlemessage.pc.loginuser.get ✅"""
        d = await self._call("mtop.taobao.idlemessage.pc.loginuser.get")
        uid = id_str(d, "userId")
        if not uid:
            raise MtopError("mtop.taobao.idlemessage.pc.loginuser.get", ["FAIL_SYS_SESSION_EXPIRED::未返回 userId"], d)
        self.self_id = uid
        return LoginUser(user_id=uid, nick=str(pick(d, "nick", "userNick", default="")))

    async def user_profile(self, user_id: str | None = None) -> UserProfile:
        """A2 用户资料：昵称与头像。

        查**别人**必须用 `mtop.idle.web.user.page.head` + userId（实测唯一有效）。
        `mtop.idle.web.user.page.nav` 无论传什么 userId 都只返回登录用户自己 ——
        用它补会话头像会让所有会话都显示成自己的头像和昵称（D-20）。
        查自己仍用 page.nav 不带参数。
        """
        if user_id:
            d = await self._call("mtop.idle.web.user.page.head", "1.0", {"userId": str(user_id)})
        else:
            d = await self._call("mtop.idle.web.user.page.nav", "1.0", {})
        base = pick(d, "module.base", default={})
        if not isinstance(base, dict):
            base = {}
        nick = str(pick(base, "displayName", "displayNick", default="")).strip()
        if not nick and not user_id:
            raise MtopError("mtop.idle.web.user.page.nav", ["FAIL_BIZ_PROFILE_EMPTY::未返回用户昵称"], d)
        # head 接口的 avatar 是嵌套对象：{"avatar": url, "avatarPendant": url}；
        # nav 接口是纯字符串。两种都要吃得下。
        av = base.get("avatar")
        if isinstance(av, dict):
            av = av.get("avatar") or av.get("avatarUrl") or ""
        avatar = img_url(av or "")
        return UserProfile(user_id=str(user_id or self.self_id), nick=nick,
                           account=str(pick(base, "displayNick", default="")), avatar=avatar)

    async def redpoint(self) -> int:
        """D4 mtop.taobao.idlemessage.pc.redpoint.query ✅ —— 返回未读总数。"""
        d = await self._call("mtop.taobao.idlemessage.pc.redpoint.query", "1.0",
                             {"sessionTypes": "1,19,15,32,3,44,51,52,24", "fetch": 50})
        total = pick(d, "total", "unreadCount", "count", default=None)
        if total is None:
            total = sum(int(x.get("unread") or x.get("count") or 0) for x in find_list(d, "sessionType"))
        return int(total or 0)

    # ---------------------------------------------------------------- B 商品
    async def item_list(self, page: int = 1, page_size: int = 20, group_id: str = "", group_name: str = "") -> ItemPage:
        """B7 mtop.idle.web.xyh.item.list ✅"""
        data = {"needGroupInfo": True, "pageNumber": page, "pageSize": page_size,
                "userId": self.self_id or "", "groupName": group_name or "", "groupId": group_id or ""}
        d = await self._call("mtop.idle.web.xyh.item.list", "1.0", data)
        items = []
        for card in d.get("cardList") or []:
            c = card.get("cardData") or {}
            st = int(c.get("itemStatus") or 0)
            items.append(Item(
                item_id=id_str(c, "id", "itemId"), title=c.get("title") or "",
                price=yuan(pick(c, "priceInfo.price", default="")), status=st, status_text=ITEM_STATUS.get(st, str(st)),
                pic_url=img_url(pick(c, "picInfo.picUrl", default="")
                                or first_image(c.get("detailParams") or {})),
                category_id=id_str(c, "categoryId")))
        groups = [ItemGroup(group_id=id_str(g, "groupId"), name=g.get("groupName") or "", count=int(g.get("itemNumber") or 0))
                  for g in d.get("itemGroupList") or []]
        return ItemPage(items=items, groups=groups, total=int(d.get("totalCount") or len(items)),
                        has_next=bool(d.get("nextPage")))

    async def item_management_query(self, page: int = 1, page_size: int = 20, extra: dict | None = None) -> dict:
        """B7 mtop.taobao.idle.item.management.query（H5 我发布的，待联调）—— 返回原始 data。"""
        return await self._call("mtop.taobao.idle.item.management.query", "1.0",
                                {"pageNumber": page, "rowsPerPage": page_size, **(extra or {})})

    async def item_detail(self, item_id: str) -> dict:
        """mtop.taobao.idle.pc.detail ✅ —— 返回原始 data。"""
        return await self._call("mtop.taobao.idle.pc.detail", "1.0", {"itemId": str(item_id)})

    async def badwords_check(self, title: str, desc: str = "") -> list[str]:
        """B2 mtop.taobao.idleitem.badwords.prepubcheck v2.0（待联调）—— 返回命中词。"""
        d = await self._call("mtop.taobao.idleitem.badwords.prepubcheck", "2.0",
                             {"title": title, "desc": desc, "scene": "publish", "sessionId": secrets.token_hex(8)})
        words = pick(d, "hitWords", "badWords", "words", default=[])
        if isinstance(words, str):
            words = [w for w in words.split(",") if w]
        return [w if isinstance(w, str) else str(w.get("word") or w) for w in words]

    async def property_recommend(self, title: str, extra: dict | None = None) -> list[PropSuggestion]:
        """B1 mtop.taobao.idle.kgraph.property.recommend v2.0（待联调）"""
        d = await self._call("mtop.taobao.idle.kgraph.property.recommend", "2.0",
                             {"title": title, "lockCpv": False, "multiSKU": False,
                              "publishScene": "pcMainPublish", "scene": "newPublishChoice", **(extra or {})})
        out = []
        for p in find_list(d, "propertyName") or find_list(d, "name"):
            name = str(p.get("propertyName") or p.get("name") or "")
            val = str(p.get("valueName") or p.get("value") or pick(p, "values.0.valueName", default=""))
            if name:
                out.append(PropSuggestion(name=name, value=val, cpv=p))
        return out

    @staticmethod
    def _publish_payload(form: PublishForm, unique_code: str) -> dict:
        payload = {
            "title": form.title, "desc": form.desc,
            "price": str(int(round(form.price * 100))),       # 分
            "origPrice": str(int(round((form.orig_price or 0) * 100))) if form.orig_price else "",
            "quantity": str(form.quantity),
            "imageInfos": [{"url": u, "major": i == 0} for i, u in enumerate(form.images)],
            "catId": form.category_id, "cpvList": form.props,
            "uniqueCode": unique_code, "bizcode": "pcMainPublish", "publishScene": "pcMainPublish",
        }
        payload.update(form.extra)
        return payload

    async def publish(self, form: PublishForm, unique_code: str | None = None) -> str:
        """B1 mtop.idle.pc.idleitem.publish（待联调）—— uniqueCode 为幂等键，重试必须复用。返回 itemId。"""
        unique_code = unique_code or secrets.token_hex(16)
        d = await self._call("mtop.idle.pc.idleitem.publish", "1.0", self._publish_payload(form, unique_code))
        return str(pick(d, "itemId", "item.itemId", "id", default=""))

    async def edit_detail(self, item_id: str) -> dict:
        """B4 mtop.idle.pc.idleitem.editDetail（待联调）—— 编辑回填原始数据。"""
        return await self._call("mtop.idle.pc.idleitem.editDetail", "1.0", {"itemId": str(item_id)})

    async def edit(self, item_id: str, form: PublishForm, unique_code: str | None = None) -> str:
        """B4 mtop.idle.pc.idleitem.edit（待联调）"""
        payload = self._publish_payload(form, unique_code or secrets.token_hex(16))
        payload["itemId"] = str(item_id)
        d = await self._call("mtop.idle.pc.idleitem.edit", "1.0", payload)
        return str(pick(d, "itemId", default=item_id))

    async def edit_price(self, item_id: str, price: float) -> str:
        """B8 批量改价的单品动作：editDetail 回填后只改价格再 edit。"""
        detail = await self.edit_detail(item_id)
        form = PublishForm(
            title=str(pick(detail, "title", "itemDO.title", default="")) or "-",
            desc=str(pick(detail, "desc", "itemDO.desc", default="")),
            price=price, quantity=int(pick(detail, "quantity", "itemDO.quantity", default=1) or 1),
            images=[x.get("url") if isinstance(x, dict) else str(x) for x in pick(detail, "imageInfos", "images", default=[])],
            category_id=str(pick(detail, "catId", "categoryId", default="")),
            extra={k: v for k, v in detail.items() if k not in {"title", "desc", "price", "quantity", "imageInfos", "images"}
                   and isinstance(v, (str, int, float, bool))},
        )
        return await self.edit(item_id, form)

    async def item_open(self, item_id: str) -> None:
        """B5 上架 mtop.idle.idleitem.item.open（待联调）"""
        await self._call("mtop.idle.idleitem.item.open", "1.0", {"itemId": str(item_id)})

    async def item_cancel(self, item_id: str) -> None:
        """B5 下架 mtop.idle.idleitem.item.cancel（待联调）"""
        await self._call("mtop.idle.idleitem.item.cancel", "1.0", {"itemId": str(item_id)})

    async def item_polish(self, item_id: str) -> None:
        """B6 擦亮 mtop.taobao.idle.item.polish（待联调）"""
        await self._call("mtop.taobao.idle.item.polish", "1.0", {"itemId": str(item_id)})

    async def draft_save(self, form: PublishForm, draft_id: str = "", unique_code: str | None = None) -> str:
        """B3 mtop.idle.idleitem.draft.edit（待联调）—— 新建或更新草稿，返回 draftId。"""
        payload = self._publish_payload(form, unique_code or secrets.token_hex(16))
        if draft_id:
            payload["draftId"] = draft_id
        d = await self._call("mtop.idle.idleitem.draft.edit", "1.0", payload)
        return str(pick(d, "draftId", "id", default=draft_id))

    async def draft_list(self) -> list[Draft]:
        """B3 mtop.idle.idleitem.draft.list（待联调）"""
        d = await self._call("mtop.idle.idleitem.draft.list", "1.0", {})
        return [Draft(draft_id=id_str(x, "draftId", "id"), title=str(x.get("title") or ""),
                      updated=str(x.get("gmtModified") or x.get("updateTime") or ""))
                for x in find_list(d, "draftId")]

    async def draft_publish(self, draft_id: str) -> str:
        """B3 mtop.idle.idleitem.draft.publish（待联调）—— 返回 itemId。"""
        d = await self._call("mtop.idle.idleitem.draft.publish", "1.0", {"draftId": str(draft_id)})
        return str(pick(d, "itemId", default=""))

    async def draft_delete(self, draft_id: str) -> None:
        """B3 mtop.idle.idleitem.draft.delete（待联调）"""
        await self._call("mtop.idle.idleitem.draft.delete", "1.0", {"draftId": str(draft_id)})

    async def upload_image(self, filename: str, content: bytes) -> UploadResult:
        """B1 图片上传（非 mtop）。过小图片平台返回 PICTURE_NOT_SAFE。"""
        res = await self.t.upload(filename, content)
        if not res.get("success"):
            code = res.get("errorCode") or "UPLOAD_FAILED"
            raise MtopError("stream-upload", [f"{code}::{res.get('message') or ''}"], res)
        url = pick(res, "object.url", "url", "data.url", default="")
        return UploadResult(url=str(url))

    # ---------------------------------------------------------------- C 订单与发货
    async def bought_list(self, page: int = 1, status: str = "ALL") -> OrderPage:
        """C1 mtop.idle.web.trade.bought.list ✅"""
        d = await self._call("mtop.idle.web.trade.bought.list", "1.0",
                             {"pageNumber": page, "orderStatus": status})
        out = []
        for it in d.get("items") or []:
            cd = it.get("commonData") or {}
            head = pick(it, "head.data", default={})
            info = pick(it, "content.data", default={})
            out.append(OrderSummary(
                order_id=id_str(cd, "orderId"), item_id=id_str(cd, "itemId"),
                title=pick(info, "detailInfo.auctionTitle", default=""), pic=img_url(pick(info, "detailInfo.auctionPic", default="")),
                price=str(pick(info, "priceInfo.price", default="")), status=str(cd.get("tradeStatusEnum") or ""),
                status_text=str(head.get("statusViewMsg") or ""), peer_nick=pick(head, "userInfo.userNick", default=""),
                created=str(head.get("createTime") or ""), is_seller=bool(cd.get("seller"))))
        return OrderPage(orders=out, total=int(d.get("totalCount") or len(out)), has_next=bool(d.get("nextPage")))

    async def order_detail(self, tid: str) -> OrderDetail:
        """C2 mtop.idle.web.trade.order.detail（入参 tid）"""
        d = await self._call("mtop.idle.web.trade.order.detail", "1.0", {"tid": str(tid)})
        buttons = []
        for b in pick(d, "bottomBarVO.buttonList", default=[]):
            ev = b.get("clickEvent") or {}
            buttons.append(OrderButton(name=str(b.get("name") or ""), action=str(b.get("tradeAction") or ""),
                                       kind=str(ev.get("type") or ""), url=str(pick(ev, "data.url", default="")),
                                       code=str(pick(ev, "data.code", default=""))))
        title = ""
        for c in d.get("components") or []:
            title = pick(c, "data.orderStatusInfo.title", default="") or title
        return OrderDetail(order_id=id_str(d, "orderId") or str(tid), item_id=id_str(d, "itemId"),
                           status=str(d.get("status") or pick(d, "utArgs.orderStatus", default="")),
                           status_title=title or str(pick(d, "utArgs.orderStatusName", default="")),
                           peer_user_id=id_str(d, "peerUserId"), buttons=buttons,
                           raw_components=[str(c.get("render")) for c in d.get("components") or []])

    async def wait_consign_list(self, page: int = 1, page_size: int = 50, extra: dict | None = None) -> WaitConsignPage:
        """C3 待发货列表。两条数据源，先接口后会话兜底。

        实测（D-24）：这个接口**必须同时**带 `accessCode` + `terminalSource`，缺一个就
        `FAIL_SYS_BIZPARAM_MISSED`，而且是链式报（补上 accessCode 才告诉你缺 terminalSource）。
        参数补齐后，没有待发货订单时它返回 `QUERY_SELLER_TRADE_ORDER_LIST_FAILED` 而不是
        空列表 —— 直接抛出去前端就弹红条，所以要把这个 ret 当"空"处理。
        """
        params = {"accessCode": "idle", "terminalSource": "4",
                  "pageSize": page_size, "pageIndex": page, **(extra or {})}
        try:
            d = await self._call("mtop.taobao.idle.logistic.wait.consign.order.query", "1.0", params)
        except MtopError as e:
            if not _is_empty_consign(e):
                raise
            # 接口说"没有"，但 PC 端这个接口本身可能被限制，用会话里的订单再确认一遍
            return await self._wait_consign_from_im(page_size)
        body = d.get("data") or d
        out = []
        for grp in body.get("consignOrderInfos") or []:
            for b in grp.get("bizOrderInfoList") or []:
                out.append(WaitConsignOrder(
                    order_id=id_str(b, "bizOrderId"), item_id=id_str(b, "auctionId"),
                    item_title=str(b.get("auctionTitle") or ""), item_pic=img_url(b.get("auctionPictUrl")),
                    buyer_nick=str(b.get("buyerNick") or ""), amount=fen_to_yuan(b.get("orderValue")),
                    quantity=int(b.get("buyAmount") or 1), pay_time=str(b.get("payTime") or ""),
                    created=str(b.get("gmtCreate") or "")))
        summary = body.get("batchConsignSummaryInfo") or {}
        total = int(summary.get("subOrderCnt") or 0) or len(out)
        return WaitConsignPage(orders=out, total=total)

    async def _wait_consign_from_im(self, limit: int = 50) -> WaitConsignPage:
        """兜底：会话里带 orderId 的订单，逐个查详情，筛出"我作为卖家且待发货"的。

        `order.detail` 实测 0.46s，6 个并发 0.53s，所以这条路够快；
        覆盖范围是"有过聊天的订单"，没有聊天记录的订单拿不到（PC 端没有卖家列表接口）。
        """
        page = await self.conversations(limit=30)
        cands = [c for c in page.conversations if c.order_id]
        if not cands:
            return WaitConsignPage(orders=[], total=0)
        sem = asyncio.Semaphore(8)

        async def one(c):
            async with sem:
                try:
                    d = await self._call("mtop.idle.web.trade.order.detail", "1.0", {"tid": c.order_id})
                except MtopError:
                    return None
                if not d.get("seller"):
                    return None          # 我是买家，不是待发货
                status_text = str(pick(d, "utArgs.orderMainTitle", default="")
                                  or _order_status_title(d))
                if not _is_wait_consign(status_text):
                    return None
                info = _order_item_info(d)
                return WaitConsignOrder(
                    order_id=c.order_id, item_id=id_str(d, "itemId") or c.item_id,
                    item_title=str(info.get("title") or c.item_title),
                    item_pic=img_url(info.get("itemMainPictCdnUrl") or c.item_pic),
                    buyer_nick=c.peer_nick or c.peer_id, amount=str(info.get("price") or ""),
                    quantity=int(info.get("buyAmount") or 1),
                    pay_time=str(pick(d, "commonInfo.createTime", default="")),
                    created=str(pick(d, "commonInfo.createTime", default="")))

        got = [x for x in await asyncio.gather(*(one(c) for c in cands[:limit])) if x]
        return WaitConsignPage(orders=got, total=len(got))

    async def consign_dummy(self, order_id: str, text: str, pics: Iterable[str] = ()) -> ShipResult:
        """C4 虚拟发货 mtop.taobao.idle.logistic.consign.dummy ✅ —— 成功判定 data.data.orderId 存在。"""
        api = "mtop.taobao.idle.logistic.consign.dummy"
        d = await self._call(api, "1.0", {"orderId": str(order_id), "tradeText": text,
                                          "picList": json.dumps(list(pics)), "newUnconsign": True, "source": "normal"})
        cid = id_str(d.get("data") or {}, "orderId")
        if not cid:
            raise MtopError(api, ["FAIL_BIZ_NO_CONSIGN_ID::未返回发货单号，视为发货失败"], d)
        return ShipResult(consign_id=cid)

    async def guess_mailno(self, mail_no: str) -> MailNoGuess:
        """C5 mtop.cainiao.tdtradebusiness.guess.mailno（待联调）"""
        d = await self._call("mtop.cainiao.tdtradebusiness.guess.mailno", "1.0", {"mailNo": mail_no})
        first = (find_list(d, "cpCode") or [d])[0]
        return MailNoGuess(cp_code=str(first.get("cpCode") or ""), cp_name=str(first.get("cpName") or ""),
                           brand_code=str(first.get("brandCode") or first.get("cpCode") or ""))

    async def consign_offline(self, order_id: str, mail_no: str, cp_code: str, brand_code: str = "",
                              address_id: str = "") -> None:
        """C5 实物发货 mtop.taobao.idle.logistic.consign.offline（待联调）"""
        await self._call("mtop.taobao.idle.logistic.consign.offline", "1.0",
                         {"orderId": str(order_id), "mailNo": mail_no, "cpCode": cp_code,
                          "brandCode": brand_code or cp_code, "addressId": address_id})

    async def create_online_order(self, item_id: str, access_code: str, extra: dict | None = None) -> dict:
        """C6 mtop.taobao.idle.logistic.create.online.order（待联调）"""
        return await self._call("mtop.taobao.idle.logistic.create.online.order", "1.0",
                                {"accessCode": access_code, "itemId": str(item_id), "terminalSource": "4", **(extra or {})})

    async def adjust_price(self, order_id: str, price: float, post_fee: float = 0) -> None:
        """C7 mtop.taobao.idle.trade.user.adjust.price（待联调，价格单位：分）"""
        await self._call("mtop.taobao.idle.trade.user.adjust.price", "1.0",
                         {"bizOrderId": str(order_id), "orderId": str(order_id),
                          "price": str(int(round(price * 100))), "postFee": str(int(round(post_fee * 100)))})

    async def close_reasons(self, order_id: str) -> list[CloseReason]:
        """C8 mtop.taobao.idle.trade.order.close.reason.get"""
        d = await self._call("mtop.taobao.idle.trade.order.close.reason.get", "1.0", {"bizOrderId": str(order_id)})
        rows = find_list(d, "reasonText") or find_list(d, "text") or find_list(d, "reason")
        return [CloseReason(code=str(r.get("reasonCode") or r.get("code") or r.get("reason") or i),
                            text=str(r.get("reasonText") or r.get("text") or r.get("reason") or ""))
                for i, r in enumerate(rows)]

    async def close_by_seller(self, order_id: str, reason: str) -> None:
        """C8 mtop.taobao.idle.trade.close.by.seller v2.0"""
        await self._call("mtop.taobao.idle.trade.close.by.seller", "2.0",
                         {"tid": str(order_id), "bizOrderId": str(order_id), "closeReason": reason})

    async def delay_confirm(self, order_id: str, days: int = 3) -> None:
        """C9 mtop.taobao.idle.trade.seller.delay.confirm"""
        await self._call("mtop.taobao.idle.trade.seller.delay.confirm", "1.0",
                         {"orderId": str(order_id), "delayTime": days})

    # ---------------------------------------------------------------- D 消息
    async def conversations(self, cursor: int = 0, limit: int = 20) -> ConversationPage:
        """D1 LWP /r/Conversation/listNewestPagination"""
        body = await self._lwp(im.ROUTE_CONV_LIST, [cursor or 9999999999999, limit])
        out = []
        for uc in body.get("userConvs") or []:
            sc = uc.get("singleChatUserConversation") or {}
            conv = sc.get("singleChatConversation") or {}
            ext = conv.get("extension") or {}
            cid = im.strip_domain(conv.get("cid"))
            if not cid:
                continue      # 没有 cid 的占位会话点进去也拉不到消息，直接跳过
            pair = [im.strip_domain(conv.get("pairFirst")), im.strip_domain(conv.get("pairSecond"))]
            peer = next((p for p in pair if p and p != self.self_id), pair[0])
            msg = pick(sc, "lastMessage.message", default={})
            kind, text, _ = im.decode_content(msg.get("content"))
            # 发送者在 message.extension.senderUserId（实测），不是 sender.uid；
            # 取错路径会让 read_status 恒为 None —— 这就是"已读状态收不到"的原因。
            sender = im.strip_domain(pick(msg, "sender.uid", "extension.senderUserId", default=""))
            nick = pick(msg, "extension.reminderTitle", default="") if sender == peer else ""
            # 已读状态：readStatus 在 lastMessage 顶层。真实取值 0/2（实测 30 个会话：
            # 0 全部 unreadCount=1，2 全部 unreadCount=0），所以 >=1 即"已读"，
            # 统一归一成 1/0，前端不用猜中间值。
            raw_read = pick(sc, "lastMessage.readStatus", default=None)
            read_status = None
            if raw_read is not None:
                try:
                    read_status = 1 if int(raw_read) >= 1 else 0
                except (TypeError, ValueError):
                    read_status = None
            out.append(Conversation(
                cid=cid, peer_id=peer, peer_nick=str(nick),
                item_id=str(ext.get("itemId") or ""), item_title=str(ext.get("itemTitle") or ""),
                item_pic=img_url(ext.get("itemMainPic")), order_id=str(ext.get("orderId") or ""),
                unread=int(sc.get("redPoint") or 0), read_status=read_status,
                last_text=text or "暂无消息", last_kind=kind,
                last_time=int(msg.get("createAt") or sc.get("modifyTime") or 0),
                last_sender_id=sender))
        return ConversationPage(conversations=out, next_cursor=int(body.get("nextCursor") or 0),
                                has_more=bool(body.get("hasMore")))

    async def messages(self, cid: str, cursor: int = 0, count: int = 30) -> MessagePage:
        """D2 LWP /r/MessageManager/listUserMessages —— 返回按时间正序。"""
        body = await self._lwp(im.ROUTE_MSG_LIST, [im.with_domain(cid), False, cursor or 9999999999999, count, False])
        out = []
        for m in body.get("userMessageModels") or []:
            msg = m.get("message") or {}
            kind, text, card = im.decode_content(msg.get("content"))
            if card and card.get("pic"):
                card["pic"] = img_url(card["pic"])
            out.append(Message(message_id=str(msg.get("messageId") or ""), cid=im.strip_domain(msg.get("cid") or cid),
                               sender_id=im.strip_domain(pick(msg, "sender.uid", "extension.senderUserId", default="")),
                               kind=kind, text=text, card=card, created=int(msg.get("createAt") or 0)))
        out.sort(key=lambda x: x.created)
        return MessagePage(messages=out, next_cursor=int(body.get("nextCursor") or 0), has_more=bool(body.get("hasMore")))

    async def send_text(self, cid: str, peer_id: str, text: str) -> str:
        """D3 LWP /r/MessageSend/sendByReceiverScope —— 返回 uuid。"""
        if not self.self_id:
            await self.login_user()
        body = im.build_text_message(cid, text, peer_id, self.self_id)
        await self._lwp(im.ROUTE_SEND, body)
        return body[0]["uuid"]

    async def quick_replies(self) -> list[QuickReply]:
        """D5 mtop.taobao.idlemessage.quickreply.list.get.v1（待联调）"""
        d = await self._call("mtop.taobao.idlemessage.quickreply.list.get.v1", "1.0", {})
        rows = find_list(d, "content") or find_list(d, "text")
        return [QuickReply(text=str(r.get("content") or r.get("text"))) for r in rows if r.get("content") or r.get("text")]

    async def send_card(self, cid: str, item_id: str) -> None:
        """D6 mtop.taobao.idlemessage.message.card.send（待联调）"""
        await self._call("mtop.taobao.idlemessage.message.card.send", "1.0",
                         {"sessionId": im.strip_domain(cid), "itemId": str(item_id)})

    async def blacklist_query(self, cid: str) -> bool:
        """D8 mtop.taobao.idlemessage.pc.blacklist.query（待联调）"""
        d = await self._call("mtop.taobao.idlemessage.pc.blacklist.query", "1.0", {"sessionId": im.strip_domain(cid)})
        return bool(pick(d, "inBlacklist", "isBlack", "result", default=False))

    async def blacklist_add(self, cid: str) -> None:
        """D8 mtop.taobao.idlemessage.pc.blacklist.add v2.0（待联调）"""
        await self._call("mtop.taobao.idlemessage.pc.blacklist.add", "2.0", {"sessionId": im.strip_domain(cid)})

    async def blacklist_remove(self, cid: str) -> None:
        """D8 mtop.taobao.idlemessage.pc.blacklist.remove（待联调）"""
        await self._call("mtop.taobao.idlemessage.pc.blacklist.remove", "1.0", {"sessionId": im.strip_domain(cid)})

    # ---------------------------------------------------------------- E 售后
    async def refund_list(self, page: int = 1, page_size: int = 20, search: dict | None = None) -> RefundPage:
        """E1 mtop.taobao.idle.merchant.refund.list"""
        d = await self._call("mtop.taobao.idle.merchant.refund.list", "1.0",
                             {"pageNumber": page, "rowsPerPage": page_size, "refundSearchParam": search or {}})
        out = []
        for r in find_list(d, "refundId"):
            st = int(pick(r, "refundStatus", "status", default=0) or 0)
            out.append(Refund(refund_id=id_str(r, "refundId"), order_id=id_str(r, "orderId", "bizOrderId"),
                              item_title=str(pick(r, "itemTitle", "auctionTitle", "title", default="")),
                              amount=yuan(pick(r, "refundFee", "refundAmount", "amount", default="")),
                              status=st, status_text=str(pick(r, "statusText", "refundStatusDesc", default=REFUND_STATUS.get(st, ""))),
                              reason=str(pick(r, "reason", "refundReason", default="")),
                              created=str(pick(r, "gmtCreate", "applyTime", default=""))))
        return RefundPage(refunds=out, total=int(pick(d, "totalCount", "total", default=len(out)) or 0),
                          has_next=bool(pick(d, "nextPage", "hasNext", default=False)))

    async def refund_detail(self, refund_id: str, order_id: str) -> RefundDetail:
        """E2 mtop.taobao.idle.refund.detail ✅"""
        d = await self._call("mtop.taobao.idle.refund.detail", "1.0", {"refundId": str(refund_id), "orderId": str(order_id)})
        body = d.get("data") or d
        st = int(body.get("refundStatus") or 0)
        detail = RefundDetail(refund_id=id_str(body, "refundId") or str(refund_id), order_id=id_str(body, "orderId") or str(order_id),
                              item_id=id_str(body, "itemId"), status=st, status_text=REFUND_STATUS.get(st, str(st)),
                              idle_status=str(body.get("idleRefundStatus") or ""),
                              detail_url=REFUND_DETAIL_PAGE.format(order_id=order_id, refund_id=refund_id))
        for comp in body.get("components") or []:
            render, cd = comp.get("render"), comp.get("data")
            if render == "bottomBar":
                detail.actions = self.parse_refund_actions(cd)
            elif render == "nodeStatusInfo" and isinstance(cd, dict):
                detail.nodes = [str(n.get("title") or n.get("text") or n) for n in cd.get("nodeStatusList") or []]
            elif render == "refundInfo" and isinstance(cd, dict):
                detail.amount = yuan(pick(cd, "refundFee", "refundAmount", "amount", default="")) or detail.amount
                detail.reason = str(pick(cd, "reason", "refundReason", default=""))
            elif render == "refundStatusInfo" and isinstance(cd, dict):
                detail.status_text = str(pick(cd, "title", "statusTitle", default=detail.status_text))
        return detail

    @staticmethod
    def parse_refund_actions(data: Any) -> list[RefundAction]:
        """解析服务端下发的按钮：clickEvent.type=api → apiName/apiVersion/params；url → 跳转。"""
        rows = data if isinstance(data, list) else (data or {}).get("buttonList") or (data or {}).get("buttons") or []
        out = []
        for b in rows:
            ev = b.get("clickEvent") or {}
            ed = ev.get("data") or {}
            kind = ev.get("type")
            if kind == "api" and ed.get("apiName"):
                out.append(RefundAction(code=str(b.get("code") or ""), name=str(b.get("name") or ""), kind="api",
                                        api_name=str(ed["apiName"]), api_version=str(ed.get("apiVersion") or "1.0"),
                                        params=ed.get("params") or {}))
            elif kind in ("url", "openPage") and ed.get("url"):
                out.append(RefundAction(code=str(b.get("code") or ""), name=str(b.get("name") or ""), kind="url",
                                        url=str(ed["url"])))
            else:
                out.append(RefundAction(code=str(b.get("code") or ""), name=str(b.get("name") or ""), kind="other"))
        return out

    async def refund_action(self, action: RefundAction) -> dict:
        """E4 按服务端下发的 apiName 动态调用。agree.refund 需要支付密码 authToken，禁止走此通道。"""
        if action.kind != "api":
            raise ValueError("只有 api 类按钮可以直接调用")
        if action.api_name == "mtop.taobao.idle.refund.agree.refund":
            raise ValueError("同意退款需本人输入支付密码，请在浏览器窗口内操作")
        return await self._call(action.api_name, action.api_version, action.params)

    async def xiaoer_message(self, refund_id: str, order_id: str, content: str) -> None:
        """E5 mtop.taobao.idle.dispute.message.for.xiaoer（待联调）"""
        await self._call("mtop.taobao.idle.dispute.message.for.xiaoer", "1.0",
                         {"refundId": str(refund_id), "orderId": str(order_id), "content": content})

    # ---------------------------------------------------------------- F 评价
    async def rate_list(self, rate_type: str = "SELLER", page: int = 1, page_size: int = 20) -> RatePage:
        """F1 mtop.idle.web.trade.rate.list ✅ —— rate_type=SELLER 收到的（作为卖家），BUYER 作为买家。"""
        d = await self._call("mtop.idle.web.trade.rate.list", "1.0",
                             {"rateType": rate_type, "ratedUid": self.self_id or "", "raterType": 0,
                              "rowsPerPage": page_size, "pageNumber": page})
        out = []
        for card in d.get("cardList") or []:
            c = card.get("cardData") or {}
            out.append(Rate(rate_id=id_str(c, "rateId"), rate=int(c.get("rate") or 0), feedback=str(c.get("feedback") or ""),
                            rater_nick=str(c.get("raterUserNick") or ""), created=str(c.get("gmtCreate") or ""),
                            tags=[str(t.get("text")) for t in c.get("rateTagList") or [] if t.get("text")]))
        return RatePage(rates=out, total=int(d.get("totalCount") or len(out)), has_next=bool(d.get("nextPage")))

    async def rate_create(self, form: RateForm) -> dict:
        """F2 mtop.taobao.idle.rate.create v4.0"""
        level = "GOOD" if form.rate == 1 else "BAD"
        return await self._call("mtop.taobao.idle.rate.create", "4.0", {
            "tradeId": form.trade_id, "rate": form.rate, "feedback": form.feedback,
            "selectedTags": json.dumps({"type": level, "data": form.tags}, ensure_ascii=False),
            "imageUrls": form.image_urls, "anonymous": form.anonymous})

    # ---------------------------------------------------------------- G 统计
    async def shop_exposure(self) -> Exposure:
        """G1 mtop.taobao.idle.shop.exposure（待联调）"""
        d = await self._call("mtop.taobao.idle.shop.exposure", "1.0", {})
        return Exposure(exposure=int(pick(d, "exposure", "exposureCount", default=0) or 0),
                        views=int(pick(d, "view", "viewCount", "pv", default=0) or 0), raw=d)

    # ---------------------------------------------------------------- 人工入口
    async def open_refund_page(self, refund_id: str, order_id: str) -> str:
        """E3 同意退款：只打开官方页面，由本人输入支付密码。"""
        url = REFUND_DETAIL_PAGE.format(order_id=order_id, refund_id=refund_id)
        await self.t.open_page(url)
        return url
