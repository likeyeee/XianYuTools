"""封装层的入参 / 返回类型定义。

约定：所有 ID（itemId / orderId / tid / refundId / userId / cid）一律为 str，
19 位订单号不经过任何数值类型，避免精度丢失（任务书 5.5）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _s(v: Any) -> str:
    return "" if v is None else str(v)


class IdModel(BaseModel):
    """把常见的 ID 字段在入模型时强制转为字符串。"""

    @field_validator("*", mode="before")
    @classmethod
    def _ids_to_str(cls, v, info):
        name = info.field_name or ""
        if (name.endswith("_id") or name in {"id", "tid", "cid"}) and isinstance(v, int):
            return str(v)
        return v


# ----------------------------------------------------------------- 账号
class LoginUser(IdModel):
    user_id: str
    nick: str = ""


class UserProfile(IdModel):
    user_id: str = ""
    nick: str = ""
    account: str = ""
    avatar: str = ""


# ----------------------------------------------------------------- 商品
ITEM_STATUS = {0: "在售", 1: "已售出", 2: "已下架", -1: "已删除"}


class Item(IdModel):
    item_id: str
    title: str
    price: str = ""
    status: int = 0
    status_text: str = ""
    pic_url: str = ""
    category_id: str = ""


class ItemGroup(IdModel):
    group_id: str
    name: str
    count: int = 0


class ItemPage(BaseModel):
    items: list[Item]
    groups: list[ItemGroup] = Field(default_factory=list)
    total: int = 0
    has_next: bool = False


class PublishForm(BaseModel):
    """发布 / 编辑商品的业务表单，由封装层转为平台入参。"""

    title: str = Field(min_length=1, max_length=60)
    desc: str = Field(default="", max_length=5000)
    price: float = Field(gt=0, le=1_000_000)
    orig_price: float | None = Field(default=None, ge=0)
    quantity: int = Field(default=1, ge=1, le=9999)
    images: list[str] = Field(default_factory=list)
    category_id: str = ""
    props: list[dict] = Field(default_factory=list)   # 属性推荐返回的 cpv
    extra: dict = Field(default_factory=dict)         # 联调期透传的额外字段


class Draft(IdModel):
    draft_id: str
    title: str = ""
    updated: str = ""


class PropSuggestion(BaseModel):
    name: str
    value: str
    cpv: dict = Field(default_factory=dict)


# ----------------------------------------------------------------- 订单 / 发货
class WaitConsignOrder(IdModel):
    order_id: str                 # bizOrderId（发货接口用）
    item_id: str = ""
    item_title: str = ""
    item_pic: str = ""
    buyer_nick: str = ""
    amount: str = ""              # 元（orderValue 按分换算）
    quantity: int = 1
    pay_time: str = ""
    created: str = ""


class WaitConsignPage(BaseModel):
    orders: list[WaitConsignOrder]
    total: int


class OrderButton(BaseModel):
    name: str
    action: str = ""
    kind: str = ""                # openPage / actionCode / api
    url: str = ""
    code: str = ""


class OrderSummary(IdModel):
    order_id: str                 # orderIdStr
    item_id: str = ""
    title: str = ""
    pic: str = ""
    price: str = ""
    status_text: str = ""
    status: str = ""
    peer_nick: str = ""
    created: str = ""
    is_seller: bool = False


class OrderPage(BaseModel):
    orders: list[OrderSummary]
    total: int = 0
    has_next: bool = False


class OrderDetail(IdModel):
    order_id: str
    item_id: str = ""
    status: str = ""
    status_title: str = ""
    peer_user_id: str = ""
    buttons: list[OrderButton] = Field(default_factory=list)
    raw_components: list[str] = Field(default_factory=list)


class ShipResult(IdModel):
    consign_id: str               # data.data.orderId：发货单号，不是订单号


class MailNoGuess(BaseModel):
    cp_code: str = ""
    cp_name: str = ""
    brand_code: str = ""


class CloseReason(BaseModel):
    code: str
    text: str


# ----------------------------------------------------------------- 消息
class Conversation(IdModel):
    cid: str                      # 不带 @goofish
    peer_id: str
    peer_nick: str = ""
    peer_avatar: str = ""         # 对方头像（单独查 user.page.nav 得到，会话结构里没有）
    item_id: str = ""
    item_title: str = ""
    item_pic: str = ""
    order_id: str = ""
    unread: int = 0
    # 1 = 自己发的最后一条已被对方读了，0 = 还没读；None 表示最后一条不是自己发的
    read_status: int | None = None
    last_text: str = ""
    last_kind: str = "text"
    last_time: int = 0
    last_sender_id: str = ""


class ConversationPage(BaseModel):
    conversations: list[Conversation]
    next_cursor: int = 0
    has_more: bool = False


class Message(IdModel):
    message_id: str
    cid: str
    sender_id: str
    kind: str = "text"
    text: str = ""
    card: dict | None = None      # 卡片消息的标题/图片/价格/商品或订单号
    created: int = 0


class MessagePage(BaseModel):
    messages: list[Message]       # 按时间正序
    next_cursor: int = 0
    has_more: bool = False


class QuickReply(BaseModel):
    text: str


# ----------------------------------------------------------------- 售后
REFUND_STATUS = {1: "待卖家处理", 2: "待买家退货", 3: "待卖家收货", 4: "退款关闭", 5: "退款成功", 6: "卖家已拒绝"}


class Refund(IdModel):
    refund_id: str
    order_id: str
    item_title: str = ""
    amount: str = ""
    status: int = 0
    status_text: str = ""
    reason: str = ""
    created: str = ""


class RefundPage(BaseModel):
    refunds: list[Refund]
    total: int = 0
    has_next: bool = False


class RefundAction(BaseModel):
    """refund.detail 的 bottomBar 按钮。kind=api 时可按 api_name 动态调用（任务书 5.2）。"""

    code: str = ""
    name: str
    kind: Literal["api", "url", "other"] = "other"
    api_name: str = ""
    api_version: str = "1.0"
    params: dict = Field(default_factory=dict)
    url: str = ""


class RefundDetail(IdModel):
    refund_id: str
    order_id: str
    item_id: str = ""
    status: int = 0
    status_text: str = ""
    idle_status: str = ""
    amount: str = ""
    reason: str = ""
    nodes: list[str] = Field(default_factory=list)
    actions: list[RefundAction] = Field(default_factory=list)
    detail_url: str = ""


# ----------------------------------------------------------------- 评价
class Rate(IdModel):
    rate_id: str
    rate: int                     # 1 好评 0 中评 -1 差评
    feedback: str = ""
    rater_nick: str = ""
    created: str = ""
    tags: list[str] = Field(default_factory=list)


class RatePage(BaseModel):
    rates: list[Rate]
    total: int = 0
    has_next: bool = False


class RateForm(BaseModel):
    trade_id: str
    rate: Literal[1, 0, -1] = 1
    feedback: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    anonymous: bool = False


# ----------------------------------------------------------------- 统计
class Exposure(BaseModel):
    exposure: int = 0
    views: int = 0
    raw: dict = Field(default_factory=dict)


class UploadResult(BaseModel):
    url: str
