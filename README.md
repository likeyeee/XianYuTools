# XianYuTools · 闲鱼多店铺管理工具

Windows 单机工具：一套界面管理多个**本人所有**的闲鱼账号，覆盖商品、订单发货、客服消息、售后、评价与统计，
批量操作统一进入限速队列。系统仅通过真实浏览器连接闲鱼，不提供演示模式。

> 本仓库：<https://github.com/likeyeee/XianYuTools>

```bash
start.bat           # 启动（真实浏览器模式，需安装 Google Chrome）
run-tests.bat       # 自动化测试（102 项，使用独立 Mock，不连接真实账号）
```

## 研发过程文档

需求分析、原型设计、架构设计、测试用例、技术预研与测试报告保存在本地私有目录，**不随仓库分发**。

## 代码结构

```
shopmgr/
├── xianyu/              接口封装层（独立可复用，不依赖 DB/Web）
│   ├── client.py          XianyuClient：每个平台接口一个方法，Pydantic 类型
│   ├── models.py          入参/返回类型（ID 一律 str，杜绝 19 位订单号精度丢失）
│   ├── im.py              IM 消息模型构造与解码
│   ├── errors.py          ret 码分类（成功/失效/风控/封禁/业务/网络）
│   ├── transport_browser.py  真实浏览器：页面内 lib.mtop / IM 长连接 / 上传
│   └── transport_mock.py     自动化测试用的内存店铺（生产入口不使用）
├── scheduler/           令牌桶限速、指数退避、持久化任务队列、可入队操作注册表
├── services/            账号运行时与生命周期、自动回复、统计、设置
└── web/                 FastAPI 路由 + 原生 JS 单页前端
tests/                   单元 / 集成 / 接口 / 浏览器桥测试与脱敏夹具
```

## 功能与封装方法对照

| 编号 | 功能 | 封装方法 |
|---|---|---|
| A1 | 应用内扫码登录 | `start_qr_login` `poll_qr_login` |
| A2 | 登录态检测 | `login_user` |
| A3 | 真实昵称与头像 | `user_profile` |
| B1/B2 | 发布、违禁词、属性推荐、上传 | `publish` `badwords_check` `property_recommend` `upload_image` |
| B3 | 草稿 | `draft_save/list/publish/delete` |
| B4/B8 | 编辑、改价 | `edit_detail` `edit` `edit_price` |
| B5/B6 | 上下架、擦亮 | `item_open` `item_cancel` `item_polish` |
| B7 | 商品列表（含封面图） | `item_list` |
| C1/C2 | 订单列表、详情 | `bought_list` `order_detail` |
| C3 | 待发货 | `wait_consign_list` |
| C4/C5 | 虚拟 / 实物发货 | `consign_dummy` `guess_mailno` `consign_offline` |
| C6–C9 | 上门取件、改价、关闭、延长收货 | `create_online_order` `adjust_price` `close_reasons` `close_by_seller` `delay_confirm` |
| D1–D3 | 会话、消息、发送 | `conversations` `messages` `send_text` |
| D4–D8 | 红点、快捷回复、卡片、黑名单 | `redpoint` `quick_replies` `send_card` `blacklist_*` |
| E1–E5 | 退款列表/详情、动态按钮、打开官方页、小二留言 | `refund_list` `refund_detail` `refund_action` `open_refund_page` `xiaoer_message` |
| F1/F2 | 评价 | `rate_list` `rate_create` |
| G1 | 曝光 | `shop_exposure` |

每个封装方法在 `shopmgr/xianyu/client.py` 里都有独立实现与类型定义，入参以方法签名为准。

## 与任务书的差异（重要）

任务书 2.2 / 4.2 建议隐藏浏览器自动化特征、每账号配代理 IP、伪造设备指纹。当前实现：

- **已做**：去掉 Playwright 的 `--enable-automation`、加 `--disable-blink-features=AutomationControlled`，
  并按账号派生稳定指纹（UA / 屏幕 / DPR / 并发数 / 内存 / 触摸点 / WebGL），见 `xianyu/stealth.py`；
- **未做**：每账号代理 IP（需要外部代理资源，与"本人账号本人使用"场景不符）；
- 不处理验证码、不绕过平台安全验证：命中风控时暂停账号，提示点「窗口」手动处理。

账号浏览器默认**无头**：所有平台操作都在后台无头页面里完成，管理页是唯一界面，不会弹出任何 Chrome 窗口。
需要人工过滑块 / 输支付密码时，点账号页的「窗口」会临时切出该账号的浏览器，关掉窗口即回到无窗口状态。
