# -*- coding: utf-8 -*-
"""跨平台中文字体探测（Windows 微软雅黑 / macOS PingFang / Linux Noto 等）。

绘图脚本统一调用 configure_zh_font()：按常见中文字体名在系统字体库中查找，
找到就优先使用；找不到时回退 DejaVu Sans（中文可能显示为方框，但不会崩溃，
也保证 CI/Linux 等无中文字体的环境可正常出图）。
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

_ZH_KEYWORDS = (
    "YaHei", "SimHei", "Hei", "Noto", "WenQuanYi", "Kai", "Song",
    "PingFang", "Arial Unicode", "Source Han", "MiSans", "HarmonyOS Sans",
    "Droid Sans Fallback",
)

_configured = False


def configure_zh_font() -> None:
    """探测可用的中文字体并设置 matplotlib 全局字体；只执行一次。"""
    global _configured
    if _configured:
        return
    fonts: list[str] = []
    try:
        fonts = [
            f.name for f in fm.fontManager.ttflist
            if any(k.lower() in (f.name or "").lower() for k in _ZH_KEYWORDS)
        ]
        fonts = list(dict.fromkeys(fonts))  # 去重保序
    except Exception:
        fonts = []
    plt.rcParams["font.sans-serif"] = fonts + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    _configured = True
