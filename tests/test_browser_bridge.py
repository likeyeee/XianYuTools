"""浏览器桥集成测试：用真实 BrowserTransport + 无头 Chrome 访问本地页面（不访问闲鱼）。

本地页面提供伪造的 window.lib.mtop 与一个 WebSocket 服务，验证：
- mtop 成功 / reject 时 ret 回传（风控分类）；
- init script 自动收录页面创建的 WebSocket，LWP 按 mid 配对，经 XianyuClient 解析会话；
- 上传通过页面内 fetch 发出。
需要本机安装 Chrome；未安装时跳过。
"""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shopmgr.xianyu import MtopError, RetKind, XianyuClient
from shopmgr.xianyu.transport_browser import BrowserTransport

pytestmark = pytest.mark.anyio

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
if not CHROME.exists() and not shutil.which("chrome"):
    pytest.skip("未安装 Chrome", allow_module_level=True)

PAGE = """<html><body><script>
window.lib = {mtop: {request: function (o) {
  if (o.api === 'risk.api') return Promise.reject({ret: ['RGV587_ERROR::SM::被挤爆啦']});
  if (o.api === 'mtop.taobao.idlemessage.pc.loginuser.get') return Promise.resolve({ret: ['SUCCESS::调用成功'], data: {userId: 2200000001}});
  return Promise.resolve({api: o.api, ret: ['SUCCESS::调用成功'], data: {echo: o.data}});
}}};
window.__sock = new WebSocket('ws://127.0.0.1:%PORT%/im');
</script></body></html>"""


@pytest.fixture
async def server():
    import websockets

    box = {}

    async def handler(ws):
        async for raw in ws:
            f = json.loads(raw)
            body = {"userConvs": [{"type": 1, "singleChatUserConversation": {"redPoint": 1, "lastMessage": {"message": {
                "createAt": 1, "sender": {"uid": "3300000001@goofish"},
                "content": {"contentType": 101, "custom": {"type": 1, "summary": "在吗"}}}},
                "singleChatConversation": {"cid": "6800000001@goofish", "pairFirst": "3300000001@goofish",
                                           "pairSecond": "2200000001@goofish", "extension": {}}}}],
                "nextCursor": 0, "hasMore": 0} if f["lwp"].endswith("listNewestPagination") else {"echo": f["body"]}
            await ws.send(json.dumps({"headers": {"mid": f["headers"]["mid"]}, "code": 200, "body": body}))

    async def http(connection, request):
        # websockets 14+ 传 (connection, Request)，旧版传 (path, headers)。
        modern = hasattr(request, "headers")
        path, headers = (request.path, request.headers) if modern else (connection, request)
        if headers.get("Upgrade", "").lower() != "websocket":
            if path.startswith("/upload"):
                headers_out = [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")]
                body = b'{"success":true,"object":{"url":"https://img/x.jpg"}}'
            else:
                headers_out = [("Content-Type", "text/html; charset=utf-8")]
                body = PAGE.replace("%PORT%", str(box["p"])).encode()
            if modern:
                return websockets.Response(200, "OK", websockets.Headers(headers_out), body)
            return 200, headers_out, body
        return None

    srv = await websockets.serve(handler, "127.0.0.1", 0, process_request=http)
    box["p"] = srv.sockets[0].getsockname()[1]
    yield box["p"]
    srv.close()


@pytest.fixture
def upload_server():
    """跨域上传桩：校验 multipart 且带 cookie 凭据（与真实上传端点同为跨域 + credentials: include）。"""
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["ctype"] = self.headers["Content-Type"]
            seen["size"] = len(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.end_headers()
            self.wfile.write(b'{"success":true,"object":{"url":"https://img/x.jpg"}}')

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/upload", seen
    srv.shutdown()


async def test_browser_transport_end_to_end(server, upload_server, tmp_path):
    base = f"http://127.0.0.1:{server}/"
    upload_url, seen = upload_server
    t = BrowserTransport(tmp_path / "profile", headless=True, home_url=base, im_url=base + "im-page",
                         upload_url=upload_url, ws_pattern=r"127\.0\.0\.1")
    try:
        c = XianyuClient(t)
        assert (await c.login_user()).user_id == "2200000001"

        res = await t.mtop("demo.api", "1.0", {"orderId": "3316400474147019790"})
        assert res["data"]["echo"]["orderId"] == "3316400474147019790"

        with pytest.raises(MtopError) as e:
            await c.call_raw("risk.api")
        assert e.value.kind is RetKind.RISK

        convs = await c.conversations()
        assert convs.conversations[0].cid == "6800000001" and convs.conversations[0].peer_id == "3300000001"

        up = await c.upload_image("a.jpg", b"\xff\xd8" + b"0" * 2048)
        assert up.url == "https://img/x.jpg"
        assert seen["ctype"].startswith("multipart/form-data") and seen["size"] > 2048
    finally:
        await t.close()
    assert not t.running
