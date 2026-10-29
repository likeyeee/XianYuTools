"""运行配置。启动参数用环境变量（SHOPMGR_*），运营可调参数存 settings 表（见 services/settings.py）。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOPMGR_")

    gateway: Literal["browser"] = "browser"
    data_dir: Path = ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8765
    # 默认无头：账号浏览器全程不显示窗口，所有接口在无头页面里由平台自己的客户端发出。
    # 需要人工过滑块 / 输支付密码时，点账号页的「窗口」会临时切出可见浏览器（见 BrowserTransport.open_page）。
    headless: bool = True
    # 指纹伪装：按稳定种子派生 UA / 屏幕 / 硬件参数，补齐无头环境的失真特征（见 xianyu/stealth.py）
    spoof_fingerprint: bool = True
    # 指纹作用域：machine = 全机一套硬件参数（一台电脑开多个号，最像真人）；
    # account = 每个账号一套（用于刻意让账号看起来分布在不同设备上）
    fingerprint_scope: Literal["machine", "account"] = "machine"
    open_browser: bool = True       # 启动后自动打开管理页面
    workers: bool = True            # 测试时可关闭后台协程

    @property
    def db_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'shopmgr.db').as_posix()}"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def sessions_dir(self) -> Path:
        """登录态快照目录：cookie2 这类 session cookie 只在内存里，必须自己落盘。"""
        return self.data_dir / "sessions"


class Tunables(BaseModel):
    """运营可在设置页调整的参数（S3）。"""

    qps: float = Field(default=0.3, gt=0, le=0.3)              # 任务书 4.3：单账号 ≤ 0.3
    burst: int = Field(default=2, ge=1, le=5)
    backoff_base: float = Field(default=1.5, ge=0.1, le=30)
    backoff_cap: float = Field(default=60, ge=1, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    risk_pause_min: int = Field(default=10, ge=1, le=240)
    login_check_min: int = Field(default=10, ge=1, le=240)
    autoreply_cooldown_s: int = Field(default=60, ge=10, le=3600)
    autoreply_poll_s: int = Field(default=20, ge=5, le=600)
    read_cache_s: int = Field(default=30, ge=0, le=600)
    # 同一账号两次扫码登录的最小间隔：短时间反复扫码会被判成异常登录，下发短命 session
    qr_login_cooldown_s: int = Field(default=180, ge=0, le=3600)
    # 每次 mtop 调用前的随机抖动上限（毫秒），避免请求间隔过于均匀
    mtop_jitter_ms: int = Field(default=350, ge=0, le=2000)
