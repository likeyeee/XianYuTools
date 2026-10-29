"""REST 接口。读操作经 AccountRuntime 直接调用（带缓存），写操作一律入队（可 ?wait=1 同步等待）。"""
from __future__ import annotations

import io
import secrets
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..config import Tunables
from ..db import AuditLog, AutoReplyRule, QuickPhrase, Task
from ..services.accounts import STATUS_TEXT
from ..xianyu.client import img_url
from ..xianyu.models import PublishForm, RateForm, RefundAction

router = APIRouter()
MIN_IMAGE_SIDE = 400


def C(req: Request):
    return req.app.state.c


def acc_out(a) -> dict:
    # 历史行里可能存着 http:// 或 // 开头的头像地址，统一归一化后再给前端，避免混合内容被浏览器拦掉
    return {"id": a.id, "name": a.name, "avatar": img_url(a.avatar), "xy_user_id": a.xy_user_id,
            "status": a.status,
            "status_text": STATUS_TEXT.get(a.status, a.status),
            "resume_at": a.resume_at.isoformat(timespec="seconds") if a.resume_at else None,
            "last_check_at": a.last_check_at.isoformat(timespec="seconds") if a.last_check_at else None,
            "last_error": a.last_error, "autoreply_enabled": a.autoreply_enabled}


def task_out(t: Task) -> dict:
    return {"id": t.id, "batch_id": t.batch_id, "account_id": t.account_id, "op": t.op, "title": t.title,
            "status": t.status, "attempts": t.attempts, "max_attempts": t.max_attempts,
            "next_run_at": t.next_run_at.isoformat(timespec="seconds") if t.next_run_at else None,
            "last_error": t.last_error, "result": t.result,
            "updated_at": t.updated_at.isoformat(timespec="seconds") if t.updated_at else None}


async def read(req: Request, aid: int, label: str, fn, cache_key: str | None = None):
    rt = C(req).accounts.runtime(aid)
    return await rt.run(label, fn, cache_key=f"{cache_key}" if cache_key else None)


async def submit(req: Request, aid: int, op: str, params: dict, title: str, wait: bool, idem_key: str | None = None):
    c = C(req)
    c.accounts.runtime(aid)  # 账号不存在时 404
    task = c.queue.enqueue(aid, op, params, title=title, idem_key=idem_key)
    if wait:
        task = await c.queue.wait(task.id, timeout=30)
    return task_out(task)


# ====================================================================== 账号 A
class AccountIn(BaseModel):
    # 昵称不再由使用者填写：扫码成功后由 mtop.idle.web.user.page.nav 自动写入
    name: str = Field(default="", max_length=64)


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    autoreply_enabled: bool | None = None
    disabled: bool | None = None


@router.get("/accounts")
def list_accounts(req: Request):
    return [acc_out(a) for a in C(req).accounts.list()]


@router.post("/accounts")
def create_account(req: Request, body: AccountIn | None = None):
    """创建待登录账号；昵称由扫码后的闲鱼资料接口自动写入。"""
    acc = C(req).accounts.create(body.name if body else "")
    return acc_out(acc)


@router.patch("/accounts/{aid}")
def patch_account(req: Request, aid: int, body: AccountPatch):
    c = C(req)
    if c.accounts.get(aid) is None:
        raise KeyError(f"账号 {aid} 不存在")
    fields = {k: v for k, v in body.model_dump().items() if v is not None and k != "disabled"}
    if body.disabled is not None:
        fields["status"] = "disabled" if body.disabled else "new"
    c.accounts.update(aid, **fields)
    return acc_out(c.accounts.get(aid))


@router.delete("/accounts/{aid}")
async def delete_account(req: Request, aid: int, wipe: bool = False):
    """删除账号。默认保留浏览器 profile（设备身份），wipe=1 才连设备一起清除。"""
    await C(req).accounts.delete(aid, wipe_profile=wipe)
    return {"ok": True}


@router.post("/accounts/{aid}/login/qr")
async def start_qr_login(req: Request, aid: int):
    """生成应用内二维码；二维码内容来自 passport 官方接口，本地渲染为 SVG。"""
    return await C(req).accounts.start_qr_login(aid)


@router.get("/accounts/{aid}/login/qr")
async def poll_qr_login(req: Request, aid: int, token: str):
    c = C(req)
    res = await c.accounts.poll_qr_login(aid, token)
    if res.get("status") == "CONFIRMED":
        acc = c.accounts.get(aid)
        if acc is not None:
            res["account"] = acc_out(acc)
    return res


@router.delete("/accounts/{aid}/login/qr")
async def cancel_qr_login(req: Request, aid: int):
    await C(req).accounts.cancel_qr_login(aid)
    return {"ok": True}


@router.post("/accounts/{aid}/check")
async def check_account(req: Request, aid: int):
    return await C(req).accounts.check(aid)


@router.post("/accounts/{aid}/resume")
def resume_account(req: Request, aid: int):
    c = C(req)
    c.accounts.runtime(aid).resume()
    c.accounts.audit(aid, "account.resume", "人工解除风控暂停")
    return acc_out(c.accounts.get(aid))


@router.post("/accounts/{aid}/open")
async def open_account_window(req: Request, aid: int, url: str = "https://www.goofish.com/"):
    if not url.startswith(("https://www.goofish.com/", "https://h5.m.goofish.com/")):
        raise ValueError("只允许打开闲鱼官方页面")
    await C(req).accounts.runtime(aid).transport.open_page(url)
    return {"ok": True}


# ====================================================================== 商品 B
@router.get("/a/{aid}/items")
async def items(req: Request, aid: int, page: int = 1, size: int = Query(20, le=50), group: str = ""):
    return await read(req, aid, "mtop.idle.web.xyh.item.list", lambda c: c.item_list(page, size, group_name=group),
                      cache_key=f"items:{page}:{size}:{group}")


class BadwordsIn(BaseModel):
    title: str
    desc: str = ""


@router.post("/a/{aid}/items/badwords")
async def badwords(req: Request, aid: int, body: BadwordsIn):
    hits = await read(req, aid, "mtop.taobao.idleitem.badwords.prepubcheck", lambda c: c.badwords_check(body.title, body.desc))
    return {"hits": hits}


@router.post("/a/{aid}/items/props")
async def props(req: Request, aid: int, body: BadwordsIn):
    return await read(req, aid, "mtop.taobao.idle.kgraph.property.recommend", lambda c: c.property_recommend(body.title))


@router.post("/a/{aid}/upload")
async def upload(req: Request, aid: int, file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("图片不能超过 10MB")
    try:
        from PIL import Image
        w, h = Image.open(io.BytesIO(content)).size
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"无法识别的图片: {e}")
    if min(w, h) < MIN_IMAGE_SIDE:
        raise ValueError(f"图片尺寸 {w}×{h} 过小，需不小于 {MIN_IMAGE_SIDE}×{MIN_IMAGE_SIDE}（平台会拒绝 PICTURE_NOT_SAFE）")
    return await read(req, aid, "stream-upload", lambda c: c.upload_image(file.filename or "image.jpg", content))


@router.post("/a/{aid}/items/publish")
async def publish(req: Request, aid: int, form: PublishForm, wait: bool = True):
    if not form.images:
        raise ValueError("请至少上传一张图片")
    code = secrets.token_hex(16)   # uniqueCode：重试复用，平台侧去重
    return await submit(req, aid, "item.publish", {"form": form.model_dump(), "unique_code": code},
                        f"发布：{form.title}", wait, idem_key=f"publish:{aid}:{code}")


@router.get("/a/{aid}/items/{item_id}/edit")
async def edit_detail(req: Request, aid: int, item_id: str):
    return await read(req, aid, "mtop.idle.pc.idleitem.editDetail", lambda c: c.edit_detail(item_id))


@router.post("/a/{aid}/items/{item_id}/edit")
async def edit(req: Request, aid: int, item_id: str, form: PublishForm, wait: bool = True):
    return await submit(req, aid, "item.edit", {"item_id": item_id, "form": form.model_dump()}, f"编辑：{form.title}", wait)


class ItemAction(BaseModel):
    action: Literal["open", "cancel", "polish"]


ITEM_ACTION_TEXT = {"open": "上架", "cancel": "下架", "polish": "擦亮"}


@router.post("/a/{aid}/items/{item_id}/action")
async def item_action(req: Request, aid: int, item_id: str, body: ItemAction, wait: bool = True):
    return await submit(req, aid, f"item.{body.action}", {"item_id": item_id},
                        f"{ITEM_ACTION_TEXT[body.action]} {item_id}", wait)


class ItemBatch(BaseModel):
    action: Literal["open", "cancel", "polish", "price"]
    item_ids: list[str] = Field(min_length=1, max_length=200)
    price_mode: Literal["fixed", "percent"] = "fixed"
    value: float | None = None
    prices: dict[str, str] = Field(default_factory=dict)   # 按比例改价时前端传当前价格


@router.post("/a/{aid}/items/batch")
def item_batch(req: Request, aid: int, body: ItemBatch):
    """B8 无批量接口，拆成单品任务进入限速队列。"""
    c = C(req)
    c.accounts.runtime(aid)
    jobs = []
    for iid in body.item_ids:
        if body.action == "price":
            if body.value is None or body.value <= 0:
                raise ValueError("请填写有效的价格或比例")
            if body.price_mode == "fixed":
                price = round(body.value, 2)
            else:
                cur = float(body.prices.get(iid) or 0)
                if cur <= 0:
                    raise ValueError(f"商品 {iid} 缺少当前价格")
                price = round(cur * body.value / 100, 2)
            jobs.append(("item.edit_price", {"item_id": iid, "price": price}, f"改价 {iid} → ¥{price}"))
        else:
            jobs.append((f"item.{body.action}", {"item_id": iid}, f"{ITEM_ACTION_TEXT[body.action]} {iid}"))
    name = "批量改价" if body.action == "price" else f"批量{ITEM_ACTION_TEXT[body.action]}"
    batch = c.queue.create_batch(aid, f"item.{body.action}", f"{name}（{len(jobs)} 个）", jobs)
    return {"batch_id": batch.id, "total": len(jobs)}


@router.get("/a/{aid}/drafts")
async def drafts(req: Request, aid: int):
    return await read(req, aid, "mtop.idle.idleitem.draft.list", lambda c: c.draft_list())


class DraftIn(BaseModel):
    form: PublishForm
    draft_id: str = ""


@router.post("/a/{aid}/drafts")
async def save_draft(req: Request, aid: int, body: DraftIn, wait: bool = True):
    return await submit(req, aid, "draft.save", {"form": body.form.model_dump(), "draft_id": body.draft_id},
                        f"存草稿：{body.form.title}", wait)


@router.post("/a/{aid}/drafts/{draft_id}/publish")
async def publish_draft(req: Request, aid: int, draft_id: str, wait: bool = True):
    return await submit(req, aid, "draft.publish", {"draft_id": draft_id}, f"草稿发布 {draft_id}", wait)


@router.delete("/a/{aid}/drafts/{draft_id}")
async def delete_draft(req: Request, aid: int, draft_id: str, wait: bool = True):
    return await submit(req, aid, "draft.delete", {"draft_id": draft_id}, f"删除草稿 {draft_id}", wait)


# ====================================================================== 订单 C
@router.get("/a/{aid}/orders/wait-consign")
async def wait_consign(req: Request, aid: int, page: int = 1):
    c = C(req)
    res = await read(req, aid, "mtop.taobao.idle.logistic.wait.consign.order.query",
                     lambda cl: cl.wait_consign_list(page), cache_key=f"wait:{page}")
    c.stats.record_orders(aid, [(o.order_id, float(o.amount or 0), (o.pay_time or o.created or str(datetime.now()))[:10])
                                for o in res.orders])
    return res


@router.get("/a/{aid}/orders")
async def orders(req: Request, aid: int, page: int = 1, status: str = "ALL"):
    return await read(req, aid, "mtop.idle.web.trade.bought.list", lambda c: c.bought_list(page, status),
                      cache_key=f"orders:{page}:{status}")


@router.get("/a/{aid}/orders/{tid}")
async def order_detail(req: Request, aid: int, tid: str):
    return await read(req, aid, "mtop.idle.web.trade.order.detail", lambda c: c.order_detail(tid))


class ShipDummyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    pics: list[str] = Field(default_factory=list)


@router.post("/a/{aid}/orders/{order_id}/ship-dummy")
async def ship_dummy(req: Request, aid: int, order_id: str, body: ShipDummyIn, wait: bool = True):
    return await submit(req, aid, "logistic.consign_dummy", {"order_id": order_id, "text": body.text, "pics": body.pics},
                        f"虚拟发货 {order_id}", wait, idem_key=f"ship:{aid}:{order_id}")


class ShipOfflineIn(BaseModel):
    mail_no: str = Field(min_length=6, max_length=40, pattern=r"^[A-Za-z0-9]+$")
    cp_code: str = ""
    brand_code: str = ""
    address_id: str = ""


@router.post("/a/{aid}/orders/{order_id}/ship-offline")
async def ship_offline(req: Request, aid: int, order_id: str, body: ShipOfflineIn, wait: bool = True):
    return await submit(req, aid, "logistic.consign_offline", {"order_id": order_id, **body.model_dump()},
                        f"实物发货 {order_id} 运单 {body.mail_no}", wait, idem_key=f"ship:{aid}:{order_id}")


@router.get("/a/{aid}/mailno/guess")
async def guess_mailno(req: Request, aid: int, mail_no: str = Query(min_length=6, max_length=40)):
    return await read(req, aid, "mtop.cainiao.tdtradebusiness.guess.mailno", lambda c: c.guess_mailno(mail_no),
                      cache_key=f"guess:{mail_no}")


class BatchShipIn(BaseModel):
    order_ids: list[str] = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=1000)


@router.post("/a/{aid}/orders/batch-ship")
def batch_ship(req: Request, aid: int, body: BatchShipIn):
    """C10 批量虚拟发货。"""
    c = C(req)
    c.accounts.runtime(aid)
    jobs = [("logistic.consign_dummy", {"order_id": oid, "text": body.text, "pics": []}, f"虚拟发货 {oid}")
            for oid in dict.fromkeys(body.order_ids)]
    batch = c.queue.create_batch(aid, "logistic.consign_dummy", f"批量虚拟发货（{len(jobs)} 笔）", jobs)
    return {"batch_id": batch.id, "total": len(jobs)}


class AdjustPriceIn(BaseModel):
    price: float = Field(gt=0, le=1_000_000)
    post_fee: float = Field(default=0, ge=0, le=10000)


@router.post("/a/{aid}/orders/{order_id}/adjust-price")
async def adjust_price(req: Request, aid: int, order_id: str, body: AdjustPriceIn, wait: bool = True):
    return await submit(req, aid, "trade.adjust_price", {"order_id": order_id, **body.model_dump()},
                        f"改价 {order_id} → ¥{body.price}", wait)


@router.get("/a/{aid}/orders/{order_id}/close-reasons")
async def close_reasons(req: Request, aid: int, order_id: str):
    return await read(req, aid, "mtop.taobao.idle.trade.order.close.reason.get", lambda c: c.close_reasons(order_id))


class CloseIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/a/{aid}/orders/{order_id}/close")
async def close_order(req: Request, aid: int, order_id: str, body: CloseIn, wait: bool = True):
    return await submit(req, aid, "trade.close", {"order_id": order_id, "reason": body.reason}, f"关闭订单 {order_id}", wait)


class DelayIn(BaseModel):
    days: int = Field(default=3, ge=1, le=10)


@router.post("/a/{aid}/orders/{order_id}/delay")
async def delay_order(req: Request, aid: int, order_id: str, body: DelayIn, wait: bool = True):
    return await submit(req, aid, "trade.delay_confirm", {"order_id": order_id, "days": body.days},
                        f"延长收货 {order_id} {body.days} 天", wait)


# ====================================================================== 消息 D
@router.get("/a/{aid}/im/conversations")
async def conversations(req: Request, aid: int):
    c = C(req)
    page = await read(req, aid, "lwp:conversations", lambda x: x.conversations(limit=30))
    # 只挂缓存里的头像/昵称，缺失的丢后台补 —— 资料接口单个就可能 20 秒超时，
    # 同步等 10 个会让首屏卡 100 秒以上（实测 108 秒）。前端随后单独拉 /im/peers。
    todo = c.peers.attach(aid, page.conversations)
    if todo:
        c.peers.prefetch(aid, todo, c.accounts.runtime(aid).client)
    return page


@router.get("/a/{aid}/im/peers")
async def im_peers(req: Request, aid: int, ids: str = ""):
    """只读缓存的对方资料，供前端补头像（不触发任何外网请求）。"""
    return C(req).peers.cached(aid, [x for x in ids.split(",") if x])


@router.get("/a/{aid}/im/messages")
async def messages(req: Request, aid: int, cid: str, cursor: int = 0):
    return await read(req, aid, "lwp:messages", lambda c: c.messages(cid, cursor, 30))


class SendIn(BaseModel):
    cid: str = Field(min_length=1)
    peer_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=500)


@router.post("/a/{aid}/im/send")
async def send(req: Request, aid: int, body: SendIn, wait: bool = True):
    return await submit(req, aid, "im.send_text", body.model_dump(), f"消息 → {body.peer_id}", wait)


class CardIn(BaseModel):
    cid: str
    item_id: str


@router.post("/a/{aid}/im/card")
async def send_card(req: Request, aid: int, body: CardIn, wait: bool = True):
    return await submit(req, aid, "im.send_card", body.model_dump(), f"商品卡片 {body.item_id}", wait)


@router.get("/a/{aid}/im/redpoint")
async def redpoint(req: Request, aid: int):
    return {"unread": await read(req, aid, "mtop.taobao.idlemessage.pc.redpoint.query", lambda c: c.redpoint(),
                                 cache_key="redpoint")}


@router.get("/a/{aid}/im/quickreplies")
async def quick_replies(req: Request, aid: int):
    c = C(req)
    try:
        remote = [q.text for q in await read(req, aid, "mtop.taobao.idlemessage.quickreply.list.get.v1",
                                             lambda cl: cl.quick_replies(), cache_key="quickreply")]
    except Exception:  # noqa: BLE001 - 平台快捷语失败不影响本地短语
        remote = []
    with c.Session() as s:
        local = [{"id": p.id, "text": p.text} for p in s.scalars(select(QuickPhrase).order_by(QuickPhrase.id))]
    return {"platform": remote, "local": local}


class PhraseIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@router.post("/quick-phrases")
def add_phrase(req: Request, body: PhraseIn):
    with C(req).Session() as s:
        p = QuickPhrase(text=body.text)
        s.add(p)
        s.commit()
        return {"id": p.id, "text": p.text}


@router.delete("/quick-phrases/{pid}")
def del_phrase(req: Request, pid: int):
    with C(req).Session() as s:
        s.execute(delete(QuickPhrase).where(QuickPhrase.id == pid))
        s.commit()
    return {"ok": True}


@router.get("/a/{aid}/im/blacklist")
async def blacklist_query(req: Request, aid: int, cid: str):
    return {"blocked": await read(req, aid, "mtop.taobao.idlemessage.pc.blacklist.query", lambda c: c.blacklist_query(cid))}


class BlackIn(BaseModel):
    cid: str
    block: bool


@router.post("/a/{aid}/im/blacklist")
async def blacklist_set(req: Request, aid: int, body: BlackIn, wait: bool = True):
    op = "im.blacklist_add" if body.block else "im.blacklist_remove"
    return await submit(req, aid, op, {"cid": body.cid}, f"{'拉黑' if body.block else '解除拉黑'} {body.cid}", wait)


class RuleIn(BaseModel):
    account_id: int | None = None
    keyword: str = Field(min_length=1, max_length=200)
    match_type: Literal["contains", "exact", "regex"] = "contains"
    reply: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    sort: int = 0


def rule_out(r: AutoReplyRule) -> dict:
    return {"id": r.id, "account_id": r.account_id, "keyword": r.keyword, "match_type": r.match_type,
            "reply": r.reply, "enabled": r.enabled, "sort": r.sort}


@router.get("/autoreply/rules")
def list_rules(req: Request):
    with C(req).Session() as s:
        return [rule_out(r) for r in s.scalars(select(AutoReplyRule).order_by(AutoReplyRule.sort, AutoReplyRule.id))]


@router.post("/autoreply/rules")
def add_rule(req: Request, body: RuleIn):
    if body.match_type == "regex":
        import re
        try:
            re.compile(body.keyword)
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}")
    with C(req).Session() as s:
        r = AutoReplyRule(**body.model_dump())
        s.add(r)
        s.commit()
        return rule_out(r)


@router.put("/autoreply/rules/{rid}")
def update_rule(req: Request, rid: int, body: RuleIn):
    with C(req).Session() as s:
        r = s.get(AutoReplyRule, rid)
        if r is None:
            raise KeyError("规则不存在")
        for k, v in body.model_dump().items():
            setattr(r, k, v)
        s.commit()
        return rule_out(r)


@router.delete("/autoreply/rules/{rid}")
def delete_rule(req: Request, rid: int):
    with C(req).Session() as s:
        s.execute(delete(AutoReplyRule).where(AutoReplyRule.id == rid))
        s.commit()
    return {"ok": True}


@router.post("/a/{aid}/autoreply/scan")
async def autoreply_scan(req: Request, aid: int):
    return {"queued": await C(req).autoreply.scan_account(aid)}


# ====================================================================== 售后 E
@router.get("/a/{aid}/refunds")
async def refunds(req: Request, aid: int, page: int = 1):
    return await read(req, aid, "mtop.taobao.idle.merchant.refund.list", lambda c: c.refund_list(page),
                      cache_key=f"refunds:{page}")


@router.get("/a/{aid}/refunds/{refund_id}")
async def refund_detail(req: Request, aid: int, refund_id: str, order_id: str):
    return await read(req, aid, "mtop.taobao.idle.refund.detail", lambda c: c.refund_detail(refund_id, order_id))


@router.post("/a/{aid}/refunds/{refund_id}/action")
async def refund_action(req: Request, aid: int, refund_id: str, action: RefundAction, wait: bool = True):
    """E4 按服务端下发的按钮动态调用。"""
    if action.kind != "api":
        raise ValueError("只有 api 类按钮可以直接调用，链接类请在浏览器窗口打开")
    if action.api_name == "mtop.taobao.idle.refund.agree.refund":
        raise ValueError("同意退款需本人输入支付密码，请点击「去处理」在浏览器窗口内完成")
    return await submit(req, aid, "refund.action", {"action": action.model_dump()},
                        f"退款 {refund_id}：{action.name}（{action.api_name}）", wait)


@router.post("/a/{aid}/refunds/{refund_id}/open")
async def refund_open(req: Request, aid: int, refund_id: str, order_id: str):
    """E3 打开官方退款详情页，由本人输入支付密码。"""
    c = C(req)
    url = await c.accounts.runtime(aid).client.open_refund_page(refund_id, order_id)
    c.accounts.audit(aid, "refund.open", f"打开退款页 {refund_id}")
    return {"ok": True, "url": url}


class XiaoerIn(BaseModel):
    order_id: str
    content: str = Field(min_length=1, max_length=500)


@router.post("/a/{aid}/refunds/{refund_id}/xiaoer")
async def refund_xiaoer(req: Request, aid: int, refund_id: str, body: XiaoerIn, wait: bool = True):
    return await submit(req, aid, "refund.xiaoer", {"refund_id": refund_id, **body.model_dump()},
                        f"申请客服介入 {refund_id}", wait)


# ====================================================================== 评价 F
@router.get("/a/{aid}/rates")
async def rates(req: Request, aid: int, rate_type: Literal["SELLER", "BUYER"] = "SELLER", page: int = 1):
    return await read(req, aid, "mtop.idle.web.trade.rate.list", lambda c: c.rate_list(rate_type, page),
                      cache_key=f"rates:{rate_type}:{page}")


@router.post("/a/{aid}/rates")
async def create_rate(req: Request, aid: int, form: RateForm, wait: bool = True):
    return await submit(req, aid, "rate.create", {"form": form.model_dump()}, f"评价 {form.trade_id}", wait)


# ====================================================================== 统计 G / 仪表盘
@router.get("/a/{aid}/exposure")
async def exposure(req: Request, aid: int):
    return await read(req, aid, "mtop.taobao.idle.shop.exposure", lambda c: c.shop_exposure(), cache_key="exposure")


@router.get("/stats/health")
def health(req: Request):
    return C(req).stats.health()


@router.get("/stats/sales")
def sales(req: Request, account_id: int | None = None, days: int = Query(7, ge=1, le=90)):
    return C(req).stats.sales(account_id, days)


@router.get("/dashboard")
async def dashboard(req: Request):
    """待办汇总只统计「已启动浏览器且状态正常」的账号，避免为了看板拉起所有窗口。"""
    c = C(req)
    todo = {"wait_consign": 0, "unread": 0, "refund_pending": 0, "dead_tasks": c.queue.counts().get("dead", 0)}
    per_account = []
    for acc in c.accounts.list():
        rt = c.accounts.runtime_if_started(acc.id)
        row = {"account_id": acc.id, "name": acc.name, "wait_consign": None, "unread": None, "refund_pending": None}
        if rt and acc.status == "ok":
            try:
                w = await rt.run("mtop.taobao.idle.logistic.wait.consign.order.query", lambda cl: cl.wait_consign_list(1),
                                 cache_key="wait:1")
                r = await rt.run("mtop.taobao.idle.merchant.refund.list", lambda cl: cl.refund_list(1), cache_key="refunds:1")
                u = await rt.run("mtop.taobao.idlemessage.pc.redpoint.query", lambda cl: cl.redpoint(), cache_key="redpoint")
                row.update(wait_consign=w.total, unread=u, refund_pending=sum(1 for x in r.refunds if x.status == 1))
                c.stats.record_orders(acc.id, [(o.order_id, float(o.amount or 0), (o.pay_time or o.created)[:10])
                                               for o in w.orders if (o.pay_time or o.created)])
                todo["wait_consign"] += w.total
                todo["unread"] += u
                todo["refund_pending"] += row["refund_pending"]
            except Exception as e:  # noqa: BLE001 - 单账号失败不影响看板
                row["error"] = str(e)
        per_account.append(row)
    return {"todo": todo, "accounts": per_account, "health": c.stats.health(), "sales": c.stats.sales(None, 7),
            "queue": c.queue.counts(), "gateway": c.settings.gateway}


# ====================================================================== 队列 S1 / 审计 S2 / 设置 S3
@router.get("/tasks")
def tasks(req: Request, status: str | None = None, account_id: int | None = None, batch_id: int | None = None):
    return [task_out(t) for t in C(req).queue.list(status=status, account_id=account_id, batch_id=batch_id)]


@router.get("/tasks/{task_id}")
def task_get(req: Request, task_id: int):
    t = C(req).queue.get(task_id)
    if t is None:
        raise KeyError("任务不存在")
    return task_out(t)


@router.post("/tasks/{task_id}/retry")
def task_retry(req: Request, task_id: int):
    return task_out(C(req).queue.retry(task_id))


@router.get("/batches")
def batches(req: Request):
    return C(req).queue.batches()


@router.get("/audit")
def audit(req: Request, limit: int = Query(200, le=1000)):
    with C(req).Session() as s:
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return [{"id": r.id, "account_id": r.account_id, "action": r.action, "summary": r.summary,
                 "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]


@router.get("/settings")
def get_settings(req: Request):
    c = C(req)
    return {"tunables": c.tunables().model_dump(), "gateway": c.settings.gateway}


@router.put("/settings")
def put_settings(req: Request, body: Tunables):
    c = C(req)
    c.store.save(body)
    c.accounts.audit(None, "settings.save", body.model_dump_json())
    return {"tunables": c.tunables().model_dump(), "gateway": c.settings.gateway}
