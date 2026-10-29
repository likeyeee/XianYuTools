"""浏览器指纹伪装。

无头 Chrome 有三处一眼可见的特征：
    1. UA 里带 `HeadlessChrome/xxx`
    2. `navigator.webdriver === true`（Playwright 默认加 `--enable-automation`）
    3. 屏幕恒为 1280×720、`maxTouchPoints` 直接照抄宿主机

这里按账号派生一份**稳定**指纹：同一账号每次启动一致，不同账号互不相同。同一账号指纹跳变
比固定指纹更可疑，所以不做每次随机。

只补齐"无头环境缺失/失真的特征"，不伪造 canvas 噪声、不伪造时区语言（这两项本来就跟随系统），
避免出现自相矛盾的组合。
"""
from __future__ import annotations

import hashlib
import json

# 常见 Windows 台式机的显卡组合（ANGLE 字符串，与真实 Chrome 输出一致）
GPUS = [
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 2048SP Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]

# (宽, 高, devicePixelRatio)
SCREENS = [(1920, 1080, 1), (2560, 1440, 1), (1920, 1080, 1.25), (1536, 864, 1.25), (1366, 768, 1)]

CHROME_MAJOR = 156      # 与 channel="chrome" 实际版本保持一致，避免 UA 与 UA-CH 冲突


def _pick(seed: str | int, field: str, choices: list):
    """每一项用独立的哈希派生，避免"同账号各项取到同一段字节"导致的连锁碰撞。"""
    d = hashlib.sha256(f"xy-fp:{field}:{seed}".encode()).digest()
    return choices[d[0] % len(choices)]


def make_fingerprint(seed: str | int) -> dict:
    """由账号标识派生一份确定性指纹（同 seed 必得同结果）。"""
    gpu_vendor, gpu_renderer = _pick(seed, "gpu", GPUS)
    sw, sh, dpr = _pick(seed, "screen", SCREENS)
    return {
        "screen": [sw, sh],
        "dpr": dpr,
        "hardware_concurrency": _pick(seed, "cores", [4, 8, 12, 16]),
        "device_memory": _pick(seed, "mem", [4, 8, 16]),
        "max_touch_points": 0,
        "webgl_vendor": gpu_vendor,
        "webgl_renderer": gpu_renderer,
        "platform": "Win32",
        "languages": ["zh-CN", "zh"],
        "ua": (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"),
    }


def viewport_of(fp: dict) -> dict:
    """按屏幕尺寸给出一个"最大化窗口"的视口（高度扣掉标签栏/地址栏）。"""
    w, h = fp["screen"]
    return {"width": w, "height": max(600, h - 111)}


# 页面侧补丁：所有 getter 都通过 Function.prototype.toString 伪装成原生实现
JS_STEALTH = """
(() => {
    if (window.__xyfp) return;
    window.__xyfp = true;
    const fp = __FP__;

    const nativeToString = Function.prototype.toString;
    const fakes = new WeakSet();
    const asNative = (fn, name) => {
        try { Object.defineProperty(fn, "name", {value: name, configurable: true}); } catch (e) {}
        fakes.add(fn);
        return fn;
    };
    Function.prototype.toString = asNative(function toString() {
        if (fakes.has(this)) return "function " + (this.name || "") + "() { [native code] }";
        return nativeToString.call(this);
    }, "toString");

    const defGetter = (obj, prop, value) => {
        try {
            Object.defineProperty(obj, prop, {
                get: asNative(function () { return value; }, "get " + prop),
                configurable: true, enumerable: true,
            });
        } catch (e) {}
    };
    const defValue = (obj, prop, value) => {
        try {
            Object.defineProperty(obj, prop, {value: value, configurable: true, writable: true});
        } catch (e) {}
    };

    // ---- navigator
    const N = Navigator.prototype;
    defGetter(N, "webdriver", false);
    defGetter(N, "platform", fp.platform);
    defGetter(N, "hardwareConcurrency", fp.hardware_concurrency);
    defGetter(N, "deviceMemory", fp.device_memory);
    defGetter(N, "maxTouchPoints", fp.max_touch_points);
    defGetter(N, "languages", Object.freeze(fp.languages.slice()));
    defGetter(N, "language", fp.languages[0]);

    // ---- screen / 视口
    const S = Screen.prototype;
    defGetter(S, "width", fp.screen[0]);
    defGetter(S, "height", fp.screen[1]);
    defGetter(S, "availWidth", fp.screen[0]);
    defGetter(S, "availHeight", fp.screen[1] - 40);   // 任务栏
    defGetter(S, "colorDepth", 24);
    defGetter(S, "pixelDepth", 24);
    defGetter(window, "devicePixelRatio", fp.dpr);

    // ---- WebGL 厂商/型号
    const patchGL = (proto) => {
        if (!proto || !proto.getParameter) return;
        const orig = proto.getParameter;
        proto.getParameter = asNative(function getParameter(p) {
            if (p === 37445) return fp.webgl_vendor;      // UNMASKED_VENDOR_WEBGL
            if (p === 37446) return fp.webgl_renderer;    // UNMASKED_RENDERER_WEBGL
            return orig.apply(this, arguments);
        }, "getParameter");
    };
    patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
})();
"""
