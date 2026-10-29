"""可入队的写操作注册表：op 名 → 封装层方法。只有在此注册的操作能进入任务队列。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from ..xianyu import XianyuClient
from ..xianyu.models import PublishForm, RateForm, RefundAction

OpFn = Callable[..., Awaitable[Any]]
OPS: dict[str, OpFn] = {}


def op(name: str):
    def deco(fn: OpFn) -> OpFn:
        OPS[name] = fn
        return fn
    return deco


def to_jsonable(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return v.model_dump()
    if v is None:
        return {}
    if isinstance(v, (dict, list, str, int, float, bool)):
        return v
    return str(v)


@op("item.publish")
async def _publish(c: XianyuClient, form: dict, unique_code: str):
    return {"item_id": await c.publish(PublishForm(**form), unique_code)}


@op("item.edit")
async def _edit(c: XianyuClient, item_id: str, form: dict, unique_code: str = ""):
    return {"item_id": await c.edit(item_id, PublishForm(**form), unique_code or None)}


@op("item.edit_price")
async def _edit_price(c: XianyuClient, item_id: str, price: float):
    return {"item_id": await c.edit_price(item_id, price), "price": price}


@op("item.open")
async def _open(c: XianyuClient, item_id: str):
    await c.item_open(item_id)


@op("item.cancel")
async def _cancel(c: XianyuClient, item_id: str):
    await c.item_cancel(item_id)


@op("item.polish")
async def _polish(c: XianyuClient, item_id: str):
    await c.item_polish(item_id)


@op("draft.save")
async def _draft_save(c: XianyuClient, form: dict, draft_id: str = "", unique_code: str = ""):
    return {"draft_id": await c.draft_save(PublishForm(**form), draft_id, unique_code or None)}


@op("draft.publish")
async def _draft_publish(c: XianyuClient, draft_id: str):
    return {"item_id": await c.draft_publish(draft_id)}


@op("draft.delete")
async def _draft_delete(c: XianyuClient, draft_id: str):
    await c.draft_delete(draft_id)


@op("logistic.consign_dummy")
async def _ship_dummy(c: XianyuClient, order_id: str, text: str, pics: list | None = None):
    return await c.consign_dummy(order_id, text, pics or [])


@op("logistic.consign_offline")
async def _ship_offline(c: XianyuClient, order_id: str, mail_no: str, cp_code: str = "", brand_code: str = "",
                        address_id: str = ""):
    if not cp_code:
        g = await c.guess_mailno(mail_no)
        cp_code, brand_code = g.cp_code, g.brand_code
    await c.consign_offline(order_id, mail_no, cp_code, brand_code, address_id)
    return {"cp_code": cp_code}


@op("trade.adjust_price")
async def _adjust(c: XianyuClient, order_id: str, price: float, post_fee: float = 0):
    await c.adjust_price(order_id, price, post_fee)


@op("trade.close")
async def _close(c: XianyuClient, order_id: str, reason: str):
    await c.close_by_seller(order_id, reason)


@op("trade.delay_confirm")
async def _delay(c: XianyuClient, order_id: str, days: int = 3):
    await c.delay_confirm(order_id, days)


@op("im.send_text")
async def _send(c: XianyuClient, cid: str, peer_id: str, text: str):
    return {"uuid": await c.send_text(cid, peer_id, text)}


@op("im.send_card")
async def _card(c: XianyuClient, cid: str, item_id: str):
    await c.send_card(cid, item_id)


@op("im.blacklist_add")
async def _black_add(c: XianyuClient, cid: str):
    await c.blacklist_add(cid)


@op("im.blacklist_remove")
async def _black_rm(c: XianyuClient, cid: str):
    await c.blacklist_remove(cid)


@op("refund.action")
async def _refund_action(c: XianyuClient, action: dict):
    return await c.refund_action(RefundAction(**action))


@op("refund.xiaoer")
async def _xiaoer(c: XianyuClient, refund_id: str, order_id: str, content: str):
    await c.xiaoer_message(refund_id, order_id, content)


@op("rate.create")
async def _rate(c: XianyuClient, form: dict):
    return await c.rate_create(RateForm(**form))
