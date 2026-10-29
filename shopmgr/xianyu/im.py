"""IM 长连接（LWP）消息模型的构造与解析。

发送文本的字段要求（实测确认的唯一可行写法）：
contentType=101、custom.type=1、redPointPolicy=0、extension.extJson="{}"、
ctx 不为空、custom 里不传 summary、actualReceivers 必须同时包含对方与自己。
"""
from __future__ import annotations

import base64
import json
import random
import string
import time

DOMAIN = "goofish"

ROUTE_CONV_LIST = "/r/Conversation/listNewestPagination"
ROUTE_MSG_LIST = "/r/MessageManager/listUserMessages"
ROUTE_SEND = "/r/MessageSend/sendByReceiverScope"


def with_domain(uid: str) -> str:
    uid = str(uid)
    return uid if "@" in uid else f"{uid}@{DOMAIN}"


def strip_domain(uid: str | None) -> str:
    return str(uid or "").split("@", 1)[0]


def make_uuid() -> str:
    """源码 rU() 格式：'-' + 毫秒时间戳 + 随机串。"""
    return "-" + str(int(time.time() * 1000)) + "".join(random.choices(string.digits, k=1))


def make_mid() -> str:
    """headers.mid：随机数 + 毫秒时间戳 + ' 0'（中间有空格）。"""
    return f"{random.randint(0, 999)}{int(time.time() * 1000)} 0"


def encode_text(text: str) -> str:
    payload = {"contentType": 1, "text": {"text": text}}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode()


def build_text_message(cid: str, text: str, peer_id: str, self_id: str) -> list:
    """返回 sendByReceiverScope 的 body：[SendMessageModel, MessageReceiverScopeModel]。"""
    if not text:
        raise ValueError("消息内容不能为空")
    if not peer_id or not self_id:
        raise ValueError("actualReceivers 需要对方与自己的 userId")
    model = {
        "uuid": make_uuid(),
        "cid": with_domain(cid),
        "conversationType": 1,
        "content": {"contentType": 101, "custom": {"type": 1, "data": encode_text(text)}},
        "redPointPolicy": 0,
        "extension": {"extJson": "{}"},
        "ctx": {"appVersion": "1.0", "platform": "web"},
        "mtags": {},
        "msgReadStatusSetting": 1,
    }
    scope = {"actualReceivers": [with_domain(peer_id), with_domain(self_id)]}
    return [model, scope]


def _deep_pick(obj, keys: tuple[str, ...], depth: int = 0):
    """在解码后的消息体里按优先级找第一个命中的键（先找浅层，再往里找）。

    卡片消息各业务线字段名不统一（商品卡用 itemTitle/itemMainPic，订单卡用
    title/picUrl），所以不写死结构，按候选键名递归找。
    """
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return v
        for v in obj.values():
            got = _deep_pick(v, keys, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj[:5]:
            got = _deep_pick(v, keys, depth + 1)
            if got is not None:
                return got
    return None


CARD_TITLE_KEYS = ("itemTitle", "title", "mainTitle", "goodsTitle", "name")
# mainPic 是 contentType=7 商品卡的字段名（实测），必须排在兜底键前面
CARD_PIC_KEYS = ("itemMainPic", "mainPic", "picUrl", "imageUrl", "pic", "cover", "img")
CARD_PRICE_KEYS = ("priceText", "price", "salePrice", "amount")
CARD_ITEM_KEYS = ("itemId", "item_id")
CARD_ORDER_KEYS = ("orderId", "tradeId", "bizOrderId")

# 实测 contentType 取值（真实账号 30 个会话 / 189 条消息采样）：
#   1  文本        {"text": {"text": "..."}}
#   4  视频        {"video": {"snapshot", "url", "width", "height"}}
#   7  商品卡      {"itemCard": {"item": {"itemId", "mainPic", "price", "title"}, "itemTip"}}
#   14 系统提示    {"tip": {"tip": "你已确认收货，交易成功"}}
#   25 通知卡      {"dxCard": {"item": {"main": {"exContent": {"title", "subTitle", "button"}}}}}
#   26 商品动态卡  {"dxCard": {"item": {"main": {"exContent": {"descInfo": {"firstLineText",
#                                                              "imgUrl", "thirdLineText.price"}}}}}}
CT_TEXT, CT_VIDEO, CT_ITEM_CARD, CT_TIP, CT_NOTICE_CARD, CT_GOODS_CARD = 1, 4, 7, 14, 25, 26


def _card(ct: int, **kw) -> dict:
    """统一卡片结构，前端按 ct 决定布局，缺失字段一律空串。"""
    out = {"ct": ct, "title": "", "sub": "", "pic": "", "price": "",
           "item_id": "", "order_id": "", "action": ""}
    out.update({k: str(v or "").strip() for k, v in kw.items()})
    return out


def _dx_card(obj: dict, ct: int) -> dict | None:
    """动态卡片（25 通知卡 / 26 商品卡）：平台按 dxCard 模板下发，字段埋在 exContent 里。"""
    main = ((obj.get("dxCard") or {}).get("item") or {}).get("main") or {}
    ex = main.get("exContent") or {}
    desc = ex.get("descInfo") or {}
    third = desc.get("thirdLineText") or {}
    price = third.get("price") if isinstance(third, dict) else ""
    # 26 走 descInfo.firstLineText；25 直接给 exContent.title
    title = desc.get("firstLineText") or ex.get("title") or ""
    sub = desc.get("secondLineText") or ex.get("subTitle") or ""
    pic = desc.get("imgUrl") or ex.get("imgUrl") or ""
    btn = ex.get("button") or ex.get("rightButton") or ex.get("leftButton") or {}
    if not any((title, sub, pic)):
        return None
    return _card(ct, title=title, sub=sub, pic=pic, price=price, action=btn.get("text"))


def extract_card(obj: dict) -> dict | None:
    """从非文本消息体里抽出可展示的卡片信息；抽不到返回 None。

    优先按 contentType 走精确路径（字段名以线上实际响应为准），最后才退回按候选键递归找 ——
    递归找会把 exContent.descInfo 里的商品标题当成卡片标题，猜出来的位置经常是错的。
    """
    ct = obj.get("contentType")
    if ct == CT_ITEM_CARD:
        ic = obj.get("itemCard") or {}
        item = ic.get("item") or {}
        if item:
            return _card(ct, title=item.get("title"), pic=item.get("mainPic"),
                         price=item.get("price"), item_id=item.get("itemId"),
                         sub=ic.get("itemTip"))
    elif ct in (CT_NOTICE_CARD, CT_GOODS_CARD):
        got = _dx_card(obj, int(ct))
        if got:
            return got
    elif ct == CT_VIDEO:
        v = obj.get("video") or {}
        if v.get("snapshot") or v.get("url"):
            # 视频消息用首帧快照当封面，点开才看视频；列表和气泡里不加载 mp4
            return _card(ct, title="[视频]", pic=v.get("snapshot"))

    title = _deep_pick(obj, CARD_TITLE_KEYS)
    pic = _deep_pick(obj, CARD_PIC_KEYS)
    price = _deep_pick(obj, CARD_PRICE_KEYS)
    item_id = _deep_pick(obj, CARD_ITEM_KEYS)
    order_id = _deep_pick(obj, CARD_ORDER_KEYS)
    if not any((title, pic, item_id, order_id)):
        return None
    return _card(int(ct or 0), title=title, pic=pic, price=price,
                 item_id=item_id, order_id=order_id)


def decode_content(content: dict | None) -> tuple[str, str, dict | None]:
    """消息 content → (类型, 可读文本, 卡片信息)。

    类型：text / tip / card / image / unknown。
    - tip 是系统提示（"你已确认收货，交易成功"），要居中灰字，不能当成聊天气泡；
    - card 带 ct 字段，前端按 ct 决定展示成商品卡还是通知卡；
    - 认不出的类型退回 summary，列表里不会只剩一个"[非文本消息]"。
    """
    content = content or {}
    custom = content.get("custom") or {}
    summary = custom.get("summary") or ""
    data = custom.get("data")
    if data:
        try:
            obj = json.loads(base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "unknown", summary, None
        inner = obj.get("contentType")
        if inner == CT_TEXT:
            return "text", (obj.get("text") or {}).get("text", summary), None
        if inner == CT_TIP:
            tip = (obj.get("tip") or {}).get("tip") or summary
            return "tip", tip, None
        card = extract_card(obj)
        if card:
            if card["ct"] == CT_VIDEO:
                return "video", summary or card["title"], card
            # 列表预览优先用卡片标题：summary 常是平台兜底文案（"[卡片消息]"），
            # 标题才是真信息（"我已小刀，待刀成" / 商品名）。
            return "card", card["title"] or summary, card
        return f"type{inner}", summary, None
    text = (content.get("text") or {}).get("content")
    if text:
        return "text", text, None
    return "unknown", summary, None
