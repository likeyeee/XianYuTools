"""mtop 返回码分类。

`ret` 首元素格式为 `错误码::错误信息`，上层只关心它属于哪一类：
成功、登录失效、风控、业务封禁、普通业务错误、网络/桥接错误。
"""
from __future__ import annotations

from enum import Enum


class RetKind(str, Enum):
    OK = "ok"
    SESSION = "session"   # 登录态失效 → 账号标红，需重新扫码
    RISK = "risk"         # 风控 → 账号暂停 10 分钟，人工过验证
    BLOCK = "block"       # 业务封禁 → 停用账号
    BIZ = "biz"           # 普通业务错误（参数、状态不允许等）→ 不重试
    NETWORK = "network"   # 网络 / 页面桥异常 → 可重试


SESSION_CODES = {
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_TOKEN_EMPTY",
    "FAIL_SYS_TOKEN_EXOIRED",   # 官方拼写
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_ILLEGAL_ACCESS",
}
RISK_CODES = {
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
    "FAIL_SYS_TRAFFIC_LIMIT",
    "X5_SECURITY_ERROR",
}
BLOCK_CODES = {"FAIL_BIZ_BLOCK"}
# 平台侧临时故障，可以重试
TRANSIENT_CODES = {"FAIL_SYS_SERVICE_TIMEOUT", "FAIL_SYS_SERVICE_FAULT", "FAIL_SYS_HSF_THROWN_EXCEPTION",
                   "LWP_TIMEOUT", "BRIDGE_ERROR"}


def ret_code(ret: list | None) -> str:
    if not ret:
        return ""
    return str(ret[0]).split("::", 1)[0]


def ret_message(ret: list | None) -> str:
    if not ret:
        return ""
    parts = str(ret[0]).split("::")
    return parts[-1] if len(parts) > 1 else parts[0]


def classify(ret: list | None) -> RetKind:
    code = ret_code(ret)
    if not code:
        return RetKind.NETWORK
    if code == "SUCCESS":
        return RetKind.OK
    if code in SESSION_CODES:
        return RetKind.SESSION
    if code in RISK_CODES:
        return RetKind.RISK
    if code in BLOCK_CODES:
        return RetKind.BLOCK
    if code in TRANSIENT_CODES:
        return RetKind.NETWORK
    return RetKind.BIZ


class MtopError(Exception):
    """接口调用失败。保留原始 ret 与响应，便于排查。"""

    def __init__(self, api: str, ret: list | None, raw: dict | None = None):
        self.api = api
        self.ret = list(ret or [])
        self.raw = raw or {}
        self.kind = classify(self.ret)
        self.code = ret_code(self.ret) or "BRIDGE_ERROR"
        super().__init__(f"{api}: {self.ret[0] if self.ret else 'no ret'}")

    @property
    def retryable(self) -> bool:
        return self.kind is RetKind.NETWORK
