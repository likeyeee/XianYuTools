"""mtop H5 签名（沿用 xianyu-mtop/xy/sign.py）。

    sign = md5(token + "&" + t + "&" + appKey + "&" + data)

浏览器桥模式下签名由页面内 lib.mtop 完成，本模块用于离线校验、排查与单元测试。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

APP_KEY = "34839810"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def mtop_sign(token: str, t: str | int, app_key: str, data: str) -> str:
    """data 必须是最终原文字符串，不能二次序列化改变空格/顺序。"""
    return md5_hex(f"{token}&{t}&{app_key}&{data}")


def now_ms() -> str:
    return str(int(time.time() * 1000))


def parse_h5_tk(cookie_value: str | None) -> tuple[str, str]:
    """`_m_h5_tk` -> (token, expire_ts)。值格式 `token_13位时间戳`。"""
    if not cookie_value:
        return "", ""
    if "_" not in cookie_value:
        return cookie_value, ""
    token, _, ts = cookie_value.rpartition("_")
    return token, ts


def dumps_data(data: Any) -> str:
    """紧凑、保留中文。签名与请求体必须用同一个结果。"""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
