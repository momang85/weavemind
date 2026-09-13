# -*- coding: utf-8 -*-
"""首次运行引导：交互式生成 config.json。

新手此前需要自己 `copy config.example.json config.json` 再手编 JSON（llm.api_key /
base_url / model），失败率很高——start.bat 只能打印这行提示然后退出。本模块把这一步
变成问答：选供应商 → 填 key → 确认模型 → （可选）Embedding → 连通性自测 → 写盘。

设计约束：
- 纯标准库；密钥只写入被 gitignore 的 config.json，终端输出一律掩码；
- 非交互（无 tty 或 WM_NONINTERACTIVE=1）只打印指引并以非零码退出，绝不阻塞脚本/CI；
- 回环/私有/保留地址**不发起探测**（本地 LLM 仍可保存，仅跳过测试并说明）；
- 连通性判定复用 `llm_client._probe_endpoint_status`（它已区分鉴权/欠费/不可达，
  并把"推理模型吃光探测预算"视为可用）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATE_PATH = BASE_DIR / "config.example.json"

# 供应商预设：(菜单名, base_url, 推荐模型)
PROVIDERS = (
    ("SiliconFlow（默认，国内直连）", "https://api.siliconflow.cn/v1",
     "deepseek-ai/DeepSeek-V3"),
    ("DeepSeek 官方", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("自定义 base_url", "", ""),
)
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"
# 模板里的占位符：这些值等于"没填"
_PLACEHOLDER_HINTS = ("your_", "your-", "xxxx", "changeme", "placeholder")


def _t(zh: str, en: str) -> str:
    """双语输出：WM_PLAIN_TEXT=1（或 --plain）时用英文。"""
    try:
        import cli_text
        return cli_text.msg(zh, en)
    except Exception:
        return zh


def mask(secret: str) -> str:
    """密钥展示掩码（任何输出路径都不得回显明文）。"""
    s = str(secret or "")
    if not s:
        return "<empty>"
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-4:]}"


def _looks_placeholder(value: str, example: str = "") -> bool:
    v = str(value or "").strip()
    if not v:
        return True
    if v == str(example or "").strip():
        return True
    low = v.lower()
    return any(h in low for h in _PLACEHOLDER_HINTS)


def validate_base_url(url: str) -> tuple[bool, str]:
    """只接受 http/https 且带 host 的地址。"""
    from urllib.parse import urlparse
    raw = str(url or "").strip()
    if not raw:
        return False, _t("Base URL 不能为空", "Base URL is required")
    try:
        p = urlparse(raw)
    except Exception:
        return False, _t("Base URL 无法解析", "Base URL is not parseable")
    if p.scheme not in ("http", "https"):
        return False, _t("只支持 http/https 地址", "Only http/https URLs are supported")
    if not p.hostname:
        return False, _t("Base URL 缺少主机名", "Base URL has no hostname")
    return True, ""


def probe_allowed(url: str) -> tuple[bool, str]:
    """是否允许对该作者发起连通性探测。

    复用 adapters.transport 的 SSRF 守卫：回环/私有/保留地址一律**不探测**
    （本地 LLM 场景仍可保存配置，只是跳过联网自测）。
    """
    try:
        from adapters.transport import _validate_public_url
    except Exception:
        return True, ""
    if _validate_public_url(str(url or "")):
        return True, ""
    return False, _t(
        "该地址指向本机/内网（或保留地址），按安全策略不发起探测；配置仍会保存",
        "Address is local/private/reserved; probing is skipped by policy, config is still saved",
    )


def probe_endpoint(base_url: str, api_key: str, model: str,
                   timeout: float = 20.0) -> tuple[bool, str]:
    """极短请求验证端点可用；返回 (ok, 可读原因)。"""
    try:
        import llm_client
    except Exception as exc:
        return False, _t(f"无法加载 llm_client：{exc}", f"llm_client unavailable: {exc}")
    try:
        res = llm_client._probe_endpoint_status(base_url, api_key, model) or {}
    except Exception as exc:
        return False, _t(f"探测失败：{str(exc)[:120]}", f"Probe failed: {str(exc)[:120]}")
    if res.get("ok"):
        return True, _t("连通正常", "Endpoint OK")
    reason = str(res.get("reason") or "unreachable")
    mapping = {
        "unauthorized": _t("鉴权失败（key 可能无效或已过期）", "Unauthorized (bad/expired key)"),
        "insufficient_balance": _t("额度不足（需充值）", "Insufficient balance"),
        "unreachable": _t("网络不可达（检查地址/网络/代理）", "Unreachable (check URL/network/proxy)"),
    }
    return False, mapping.get(reason, reason)


def config_status(path: Path | None = None) -> tuple[bool, str]:
    """与 start.bat 的 [2/6] 闸门同义：llm.api_key/base_url/model 是否齐备且非占位符。"""
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        return False, _t("config.json 不存在", "config.json not found")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, _t(f"config.json 无法解析：{str(exc)[:80]}", f"config.json unparsable: {str(exc)[:80]}")
    llm = (cfg or {}).get("llm") or {}
    for key in ("api_key", "base_url", "model"):
        if _looks_placeholder(llm.get(key) or ""):
            return False, _t(f"llm.{key} 未填写", f"llm.{key} is missing")
    return True, ""


def build_config(template: dict, *, base_url: str, api_key: str, model: str,
                 embedding: dict | None = None) -> dict:
    """在模板副本上填 llm.*（及可选 embedding.*）。

    计划/备用端点跟随主端点：模板里的占位符 key 若原样保留会让规划调用 401，
    而设置页保存时也是这么传导的，保持一致。
    """
    cfg = json.loads(json.dumps(template))  # 深拷贝，避免改动调用方对象
    llm = cfg.setdefault("llm", {})
    llm["base_url"] = base_url
    llm["api_key"] = api_key
    llm["model"] = model
    if embedding:
        emb = cfg.setdefault("embedding", {})
        emb.update(embedding)
    for section in ("planner", "backup"):
        sec = cfg.setdefault(section, {})
        sec["base_url"] = base_url
        sec["api_key"] = api_key
        sec["model"] = model
    return cfg


def write_config(cfg: dict, path: Path | None = None) -> Path:
    """原子写入（临时文件 + os.replace），UTF-8 缩进 2。"""
    p = Path(path or CONFIG_PATH)
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_template() -> dict:
    if TEMPLATE_PATH.exists():
        try:
            return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"llm": {}, "embedding": {}, "planner": {}, "backup": {},
            "redis": {"host": "localhost", "port": 6379}, "system": {}}


def _interactive() -> bool:
    if os.environ.get("WM_NONINTERACTIVE"):
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _ask(prompt: str, default: str = "") -> str:
    hint = f"[{default}]" if default else ""
    try:
        ans = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        # 不能静默退出（实测：stdin 不是控制台时旧行为只打印横幅就结束，
        # 用户只看到一句"guided setup..."然后报错，无从判断发生了什么）
        print()
        print(_t("输入被中断（stdin 不是可交互控制台）。请在终端里运行："
                 " python setup_wizard.py",
                 "Input aborted (stdin is not an interactive console). "
                 "Run this in a terminal: python setup_wizard.py"))
        raise SystemExit(2)
    return ans or default


def _ask_secret(prompt: str) -> str:
    """隐藏输入；无 tty（如脚本喂 stdin）时退回普通读取。"""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            import getpass
            return getpass.getpass(f"{prompt}: ").strip()
    except Exception:
        pass
    try:
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print(_t("输入被中断（stdin 不是可交互控制台）。请在终端里运行："
                 " python setup_wizard.py",
                 "Input aborted (stdin is not an interactive console). "
                 "Run this in a terminal: python setup_wizard.py"))
        raise SystemExit(2)


def _print_guidance() -> None:
    print(_t(
        "当前环境无法交互，请手动执行：",
        "Non-interactive environment. Please do it manually:",
    ))
    print("  1. copy config.example.json config.json")
    print("  2. fill in llm.api_key / llm.base_url / llm.model")
    print(_t("  或设置好环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL",
             "  or set env vars LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"))


def run_interactive(force: bool = False, path: Path | None = None) -> int:
    """引导流程；返回进程退出码（0 成功）。"""
    target = Path(path or CONFIG_PATH)
    # 先打一行 ASCII 横幅：即使控制台字体渲染不了中文/输出编码异常，
    # 也能从用户截图里一眼确认"引导确实跑起来了"（实测排障吃过这个亏）。
    print()
    print("=== WeaveMind first-run setup ===")
    print(_t("=== 织光首次配置引导 ===", "(中文界面需终端支持 UTF-8)"))

    ok, why = config_status(target)
    if ok and not force:
        print(_t(f"配置已就绪（{why or 'OK'}），无需引导。",
                 "Config already usable; nothing to do."))
        return 0
    if not force and target.exists() and not ok:
        print(_t(f"检测到 config.json 不完整：{why}", f"config.json incomplete: {why}"))

    print(_t("回车即采用方括号中的默认值；Ctrl+C 可随时退出。",
             "Press Enter to accept the default in brackets; Ctrl+C to abort."))

    # 1) 供应商
    print()
    print(_t("可选端点：", "Endpoints:"))
    for i, (name, url, model) in enumerate(PROVIDERS, 1):
        suffix = f"  {url}  模型 {model}" if url else ""
        print(f"  {i}) {name}{suffix}")
    choice = _ask(_t("选择序号", "Choose number"), "1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(PROVIDERS):
            idx = 0
    except Exception:
        idx = 0
    _, preset_url, preset_model = PROVIDERS[idx]

    base_url = _ask(_t("Base URL", "Base URL"), preset_url)
    while True:
        valid, reason = validate_base_url(base_url)
        if valid:
            break
        print("  ! " + reason)
        base_url = _ask(_t("Base URL", "Base URL"), preset_url)

    # 2) API Key（不回声、不回显）
    api_key = ""
    while not api_key:
        api_key = _ask_secret(_t("API Key（输入时不显示）", "API Key (hidden)"))
        if _looks_placeholder(api_key):
            print("  ! " + _t("请填入真实密钥（模板占位符不算）",
                              "Please paste a real key (template placeholder rejected)"))
            api_key = ""

    # 3) 模型
    model = _ask(_t("模型名", "Model"), preset_model or "deepseek-chat")

    # 4) Embedding（可选）
    print()
    print(_t("Embedding（用于记忆检索，可留空跳过）：",
             "Embedding (for memory recall; Enter to skip):"))
    emb_choice = _ask(_t("1) 复用上面的端点与 key   2) 跳过（记忆检索降级为关键词兜底）",
                       "1) reuse the endpoint/key above   2) skip (keyword fallback)"), "1")
    embedding = None
    if str(emb_choice).strip().startswith("1"):
        emb_model = _ask(_t("Embedding 模型", "Embedding model"), DEFAULT_EMBEDDING_MODEL)
        embedding = {"base_url": base_url, "api_key": api_key, "model": emb_model}

    # 5) 连通性自测（回环/内网按策略跳过）
    print()
    allowed, why_skip = probe_allowed(base_url)
    if not allowed:
        print("  - " + why_skip)
    else:
        print(_t(f"正在测试 {base_url} …", f"Probing {base_url} …"))
        good, reason = probe_endpoint(base_url, api_key, model)
        print(("  ✓ " if good else "  ! ") + reason)
        if not good:
            # 默认重填：探测已给出可读原因（key 无效/额度不足/网络不通），
            # 新手在"回车即保存"的默认下极易把坏配置带进启动流程。
            again = _ask(_t("回车=重新填写 / 输入 n = 仍然保存",
                            "Enter=re-enter / n=save anyway"), "y")
            if not str(again).strip().lower().startswith("n"):
                return run_interactive(force=True, path=target)

    # 6) 写盘
    cfg = build_config(load_template(), base_url=base_url, api_key=api_key,
                       model=model, embedding=embedding)
    written = write_config(cfg, target)
    print()
    print(_t(f"已写入 {written.name}：base_url={base_url} model={model} api_key={mask(api_key)}",
             f"Wrote {written.name}: base_url={base_url} model={model} api_key={mask(api_key)}"))
    if embedding is None:
        print(_t("提示：未配置 Embedding，记忆检索会降级为关键词兜底（不影响任务执行）。",
                 "Note: no Embedding configured; memory recall degrades to keyword search."))
    print(_t("下一步：启动后打开 http://localhost:8080 ，首次登录点「创建管理员」。",
             "Next: after startup open http://localhost:8080 and create the admin account."))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--plain" in args:
        os.environ["WM_PLAIN_TEXT"] = "1"
    try:
        import cli_text
        cli_text.setup_console_encoding()
    except Exception:
        pass
    force = "--force" in args
    if "--check" in args:
        ok, why = config_status()
        print(("OK: " if ok else "MISSING: ") + (why or "config OK"))
        return 0 if ok else 1
    if not _interactive():
        ok, why = config_status()
        if ok:
            print(_t("配置已就绪。", "Config OK."))
            return 0
        _print_guidance()
        return 1
    return run_interactive(force=force)


if __name__ == "__main__":
    raise SystemExit(main())
