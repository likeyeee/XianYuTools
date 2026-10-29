"""从 xianyu-mtop/data/live 的实测样本生成脱敏夹具：保留结构与 ID 形态，替换昵称/地址/手机/IP/指纹/聊天正文。

运行：python tests/fixtures/make_fixtures.py <xianyu-mtop/data/live 目录>
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent
SENSITIVE = re.compile(r"(nick|name|mobile|phone|address|addr|ip$|clientip|umid|utdid|token|icon|avatar|head|"
                       r"reminder|notice|summary|feedback|title$|detailInfo|receiver|url$|pic|img)", re.I)
DROP = {"receiverInfo", "responseHeaders", "encryptedPhone"}
KEEP = {"auctionTitle", "itemTitle", "title", "statusViewMsg", "tradeStatusEnum", "orderStatusName", "render", "name",
        "groupName", "tabName", "buttonList", "tradeAction"}


def fake_text(n: int = 0) -> str:
    return f"测试文本{n}"


def scrub(obj, key: str = ""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP:
                continue
            if k == "custom" and isinstance(v, dict) and v.get("data"):
                text = {"contentType": 1, "text": {"text": "你好，还在吗？"}}
                v = {**v, "summary": "你好，还在吗？",
                     "data": base64.b64encode(json.dumps(text, ensure_ascii=False).encode()).decode()}
                out[k] = {kk: scrub(vv, kk) if kk not in ("data", "summary") else vv for kk, vv in v.items()}
                continue
            out[k] = scrub(v, k)
        return out
    if isinstance(obj, list):
        return [scrub(x, key) for x in obj]
    if isinstance(obj, str) and key not in KEEP and SENSITIVE.search(key):
        return "" if not obj else "已脱敏"
    if isinstance(obj, str) and key in ("auctionTitle", "itemTitle") and obj:
        return "测试商品标题"
    return obj


UID_KEYS = re.compile(r"(userid|buyerid|sellerid|peeruserid|^uid$|pairfirst|pairsecond|ratedUid|ownerUserId|extUserId)", re.I)


def collect_uids(obj, key: str = "", acc: set | None = None) -> set:
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            collect_uids(v, k, acc)
    elif isinstance(obj, list):
        for x in obj:
            collect_uids(x, key, acc)
    elif UID_KEYS.search(key) and obj not in (None, "", 0):
        m = re.match(r"(\d{6,})", str(obj))
        if m:
            acc.add(m.group(1))
    return acc


def pseudonymize(text: str, uids: set) -> str:
    """平台用户 ID 一律替换为同长度的假 ID（保持跨文件一致）。"""
    for i, uid in enumerate(sorted(uids)):
        fake = ("9" + str(100000000 + i * 7919))[: len(uid)].ljust(len(uid), "0")
        text = text.replace(uid, fake)
    return text


def main(live: Path) -> None:
    jobs = {
        "wait_consign.json": ("mtop.taobao.idle.logistic.wait.consign.order.query.json", None),
        "bought_list.json": ("trade.bought.list.json", ("data", "items", 3)),
        "order_detail.json": ("order_detail_3316400474147019790.json", None),
        "refund_detail.json": ("refund_detail.json", None),
        "rate_list.json": ("rate_list_SELLER.json", ("data", "cardList", 3)),
        "conv_list.json": ("conv_list.json", ("body", "userConvs", 2)),
        "messages.json": ("listUserMessages.json", ("body", "userMessageModels", 3)),
        "item_list.json": ("xyh.item.list.json", ("data", "cardList", 3)),
        "ship_result.json": ("ship_result.json", None),
    }
    loaded = {}
    uids: set = set()
    for out, (src, trim) in jobs.items():
        data = json.loads((live / src).read_text("utf-8"))
        if trim:
            a, b, n = trim
            data[a][b] = data[a][b][:n]
        loaded[out] = scrub(data)
        collect_uids(data, acc=uids)
    for out, data in loaded.items():
        (OUT / out).write_text(pseudonymize(json.dumps(data, ensure_ascii=False, indent=1), uids), "utf-8")
        print("wrote", out)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
