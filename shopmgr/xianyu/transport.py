"""传输层抽象。封装层只依赖这个协议，真实浏览器与 Mock 可以互换。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    async def start(self) -> None:
        """准备好可调用的页面环境。"""

    async def close(self) -> None:
        """释放浏览器等资源。"""

    async def mtop(self, api: str, v: str, data: dict) -> dict:
        """发起 mtop 调用，返回原始响应 `{api, v, ret, data}`。桥接失败时 ret 为空。"""

    async def lwp(self, route: str, body: list) -> dict:
        """发起 IM 长连接请求，返回 `{code, body, headers}`。"""

    async def upload(self, filename: str, content: bytes) -> dict:
        """上传图片，返回 `{success, errorCode, message, object:{url}}`。"""

    async def open_page(self, url: str) -> None:
        """在该账号的可见窗口中打开页面，交给使用者本人操作。"""
