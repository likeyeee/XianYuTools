"""闲鱼接口封装层：独立可复用，不依赖本项目的 DB / Web。

    from shopmgr.xianyu import XianyuClient, MockTransport
    client = XianyuClient(MockTransport())
    await client.login_user()
"""
from .client import XianyuClient
from .errors import MtopError, RetKind, classify
from .transport import Transport
from .transport_mock import MockTransport

__all__ = ["XianyuClient", "MtopError", "RetKind", "classify", "Transport", "MockTransport"]
