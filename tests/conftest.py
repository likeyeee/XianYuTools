from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shopmgr.config import Settings, Tunables  # noqa: E402
from shopmgr.web.app import Container  # noqa: E402
from shopmgr.xianyu import MockTransport  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def fast_tunables(**kw) -> Tunables:
    """测试用：放开限速、缩短退避，避免用例变慢（绕过 qps≤0.3 的校验）。"""
    base = Tunables().model_dump()
    base.update(qps=1000, burst=1000, backoff_base=0.01, backoff_cap=0.05, read_cache_s=0,
                mtop_jitter_ms=0, qr_login_cooldown_s=0)
    base.update(kw)
    return Tunables.model_construct(**base)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, open_browser=False, workers=False)


@pytest.fixture
def container(settings) -> Container:
    return Container(
        settings,
        transport_factory=lambda acc: MockTransport(user_id=str(2200000000 + acc.id), seed=acc.id),
        tunables_override=fast_tunables(),
    )


@pytest.fixture
async def acc(container):
    """一个已登录的 Mock 账号，返回 (account_id, runtime, mock_transport)。"""
    a = container.accounts.create("测试店")
    res = await container.accounts.check(a.id)
    assert res["ok"], res
    rt = container.accounts.runtime(a.id)
    return a.id, rt, rt.transport


class FixtureTransport:
    """按接口名返回实测脱敏样本，用于验证解析器对真实结构的兼容性。"""

    def __init__(self, mapping: dict[str, dict]):
        self.mapping = mapping
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def mtop(self, api, v, data):
        self.calls.append((api, data))
        return self.mapping[api]

    async def lwp(self, route, body):
        self.calls.append((route, body))
        return self.mapping[route]

    async def upload(self, filename, content):
        return {"success": True, "object": {"url": "u"}}

    async def open_page(self, url):
        self.calls.append(("open", url))
