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

import contextlib
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATE_PATH = BASE_DIR / "config.example.json"

# 供应商预设：(菜单名, base_url, 推荐模型)
# 顺序即菜单序号；最后一项是"自定义"，序号由 `_CUSTOM_INDEX` 算出来并写进提示里。
PROVIDERS = (
    ("SiliconFlow（国内直连）", "https://api.siliconflow.cn/v1",
     "deepseek-ai/DeepSeek-V3"),
    ("DeepSeek 官方", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("Tierflow 中转（地址已填，模型名见该站控制台）", "https://tierflow.cn/v1", None),
    ("自定义 base_url（中转/聚合站、公司内网网关；key 必须与地址同一家）", "", ""),
)
_CUSTOM_INDEX = next(i for i, p in enumerate(PROVIDERS, 1) if not p[1])
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


def looks_placeholder(value: str, example: str = "") -> bool:
    """公开入口：供 launcher/web_ui 判断"配置是否真的填过"（模板占位符不算填过）。

    为什么需要：config.json 缺失时配置回退到模板，模板里 `llm.api_key` 是
    `YOUR_API_KEY` 这类占位符——只做非空判断会把"没配置"当成"配置完整"，
    于是首启引导不显示、任务提交后才在鉴权上失败。
    """
    return _looks_placeholder(value, example)


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


@contextlib.contextmanager
def _quiet_probe_logging():
    """探测期间把 `llm_client` 的日志压到 ERROR。

    探测结果本来就由本模块以人话回给用户，而 `llm_client` 还会顺手打一条
    `LLM auth/balance error ... body=...` 的 WARNING——实测它插进问答提示中间
    （"…[1]: LLM auth/balance error 401 …正在测试…"挤成一行），既难看又可能
    带出上游回显的密钥片段。真正的告警在任务执行期照旧记录。
    """
    lg = logging.getLogger("llm_client")
    old = lg.level
    lg.setLevel(logging.ERROR)
    try:
        yield
    finally:
        lg.setLevel(old)


_NEXT_STEP_HINTS = {
    "unauthorized": (
        "→ 地址不变，就只能换一把**该服务商签发**的 key；如果这把 key 来自别家"
        "（中转/聚合站），请回到菜单选「{custom}) 自定义 base_url」填那家的地址。",
        "→ With this address, only a key issued by *this* provider works; "
        "if the key came from a relay/aggregator, re-run and pick "
        "\"{custom}) custom base_url\" with that provider's address.",
    ),
    "insufficient_balance": (
        "→ 到该服务商控制台充值/提额度后再试（地址与 key 都没问题，是这个账号没钱了）。",
        "→ Top up on that provider's console and retry (address and key are fine).",
    ),
    "unreachable": (
        "→ 检查网络/代理；自填地址要带 /v1，且不能多写 /chat/completions。",
        "→ Check network/proxy; a custom address must end with /v1 "
        "(not /chat/completions).",
    ),
}


def probe_endpoint(base_url: str, api_key: str, model: str,
                   timeout: float = 20.0) -> tuple[bool | None, str]:
    """极短请求验证端点可用；返回 (ok, 可读原因)。

    `ok is None` 表示**跳过**（例：依赖尚未安装）。本模块在启动脚本的 [2/6] 运行，
    而依赖要到 [4/6] 才自动安装，此时 import llm_client 会失败——那是"还没轮到"，
    不是端点有问题；当成失败会让新手卡在"重填/仍然保存"上（实测如此）。

    失败原因优先用 `llm_client` 给出的整句（带端点域名与状态码含义），这里再补一句
    **下一步怎么做**。此前只按 `reason` 枚举翻成"鉴权失败（key 可能无效或已过期）"，
    实测把"key 与地址不同源"说成了"key 过期"，新人于是反复换 key（换到正确的那家
    才发现原先的 key 从没坏）。
    """
    try:
        import llm_client
    except ImportError as exc:
        return None, _t(
            f"依赖尚未安装（{str(exc)[:60]}），跳过连通性自测；"
            "后面的依赖步骤会自动安装，装好后会再复查一次",
            f"Dependencies not installed yet ({str(exc)[:60]}); skipping the probe "
            "(they are installed automatically later and the probe runs again)")
    except Exception as exc:
        return None, _t(f"无法加载 llm_client：{str(exc)[:80]}",
                        f"llm_client unavailable: {str(exc)[:80]}")
    try:
        with _quiet_probe_logging():
            res = llm_client._probe_endpoint_status(base_url, api_key, model) or {}
    except Exception as exc:
        return False, _t(f"探测失败：{str(exc)[:120]}", f"Probe failed: {str(exc)[:120]}")
    if res.get("ok"):
        return True, _t("连通正常", "Endpoint OK")
    reason = str(res.get("reason") or "unreachable")
    generic = {
        "unauthorized": _t("鉴权失败（key 可能无效或已过期）", "Unauthorized (bad/expired key)"),
        "insufficient_balance": _t("额度不足（需充值）", "Insufficient balance"),
        "unreachable": _t("网络不可达（检查地址/网络/代理）", "Unreachable (check URL/network/proxy)"),
    }
    # `message` 是给设置页/引导看的整句人话（含端点域名）；没有时退回枚举文案，
    # 老版本/被 mock 的探测结果仍能工作。
    detail = str(res.get("message") or "").strip() or generic.get(reason, reason)
    hint = _NEXT_STEP_HINTS.get(reason)
    if hint:
        detail = f"{detail}\n    " + _t(hint[0], hint[1]).format(custom=_CUSTOM_INDEX)
    return False, detail



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


def _base_config(target: Path) -> dict:
    """写盘基底：**已有 config.json 优先**（保留 redis/system 与用户设过的高级项），
    否则用模板。

    为什么不能只用模板：引导只为 llm/planner/backup 提问，若以模板为基底写盘，
    用户此前手工设过的 `redis.port`、`system.*`、高级模型角色等会被静默清掉——
    架构要求"保留用户主动设置的高级选项"。
    """
    try:
        if Path(target).exists():
            data = json.loads(Path(target).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    return load_template()


def _interactive() -> bool:
    """是否进入问答流程。

    优先级：WM_NONINTERACTIVE=1（自动化/CI，绝不提问）> WM_WIZARD_FORCE=1
    （强制问答：stdin 被管道或包装器接管、但答案已备好时仍走完整流程）> tty 判断。
    """
    if os.environ.get("WM_NONINTERACTIVE"):
        return False
    if str(os.environ.get("WM_WIZARD_FORCE", "")).strip().lower() in ("1", "true", "yes"):
        return True
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


_ANSWER_SOURCE = None


def _answer_source():
    """答案文件迭代器（WM_WIZARD_ANSWERS）：无人值守/被包装终端里的脚本化配置。

    为什么需要它：把 start.bat 挂在管道下时，cmd 会先消费 stdin，引导拿到的
    是空输入（实测 EOF）。真实用户用控制台不受影响，但自动化部署/回归测试
    需要一个不受 stdin 影响的入口。
    """
    global _ANSWER_SOURCE
    if _ANSWER_SOURCE is not None:
        return _ANSWER_SOURCE if _ANSWER_SOURCE is not False else None
    path = str(os.environ.get("WM_WIZARD_ANSWERS") or "").strip()
    if not path:
        _ANSWER_SOURCE = False
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        _ANSWER_SOURCE = False
        return None
    _ANSWER_SOURCE = iter(lines)
    return _ANSWER_SOURCE


def _read_line(prompt: str, *, secret: bool = False) -> str:
    """读一行答案：优先答案文件，否则读控制台。"""
    src = _answer_source()
    if src is None:
        return input(f"{prompt}: ").strip()
    try:
        line = next(src)
    except StopIteration:
        print()
        print(_t("答案文件已用尽，配置未完成。", "Answer file exhausted; setup incomplete."))
        raise SystemExit(3)
    print(f"{prompt}: {'********' if secret else line.strip()}")
    return str(line).strip()


def _ask(prompt: str, default: str = "") -> str:
    hint = f"[{default}]" if default else ""
    try:
        ans = _read_line(f"{prompt}{hint}")
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
    """隐藏输入；答案文件模式下从文件读，无 tty 时退回普通读取。"""
    if _answer_source() is not None:
        return _read_line(prompt, secret=True)
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
    print(_t("  2. fill in llm.api_key / llm.base_url / llm.model"
             "（模板里端点是**空白**的：没有默认服务商，必须填你 key 那家的地址）",
             "  2. fill in llm.api_key / llm.base_url / llm.model "
             "(the template has no endpoint preset on purpose: use the host "
             "that issued your key)"))
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
        suffix = ""
        if url:
            suffix = f"  {url}"
            # 模型名留空（None）= 该站模型名因人而异，必须由使用者填
            suffix += f"  模型 {model}" if model else "  模型名待填"
        print(f"  {i}) {name}{suffix}")
    choice = ""
    while True:
        # 不给默认值：**任何**预设都不该被一个回车选中——key 与地址必须同一家，
        # 猜错只是 401（实测：新人回车用了 SiliconFlow 预设，key 却是别家的）。
        choice = _ask(_t(f"选择序号 1-{len(PROVIDERS)}（必填，没有默认值）",
                         f"Choose 1-{len(PROVIDERS)} (required, no default)"), "")
        try:
            idx = int(str(choice).strip()) - 1
        except Exception:
            idx = -1
        if 0 <= idx < len(PROVIDERS):
            break
        print("  ! " + _t(f"请输入 1 到 {len(PROVIDERS)} 之间的序号",
                          f"Enter a number from 1 to {len(PROVIDERS)}"))
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
    if preset_model is None:
        # 使用该预设但模型名不预填（各站模型名不同，猜一个只会 404）：必填
        model = ""
        while not str(model).strip():
            model = _ask(_t("模型名（必填，取该服务商控制台里列出的那个名字）",
                            "Model (required: a name listed in that provider's console)"), "")
        model = str(model).strip()
    else:
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

    # 5) 连通性自测（回环/内网按策略跳过；依赖未装也跳过而非报错）
    print()
    probe_failed = None      # 存下最后一句失败原因：写盘时要提醒"现在还不能用"
    allowed, why_skip = probe_allowed(base_url)
    if not allowed:
        print("  - " + why_skip)
    else:
        print(_t(f"正在测试 {base_url} …", f"Probing {base_url} …"))
        good, reason = probe_endpoint(base_url, api_key, model)
        if good is None:
            # 依赖未安装等"时机未到"：不算失败，也不问"重填"
            print("  - " + reason)
            print(_t("    （装好依赖后会自动复查，无需现在处理）",
                     "    (re-checked automatically after dependencies are installed)"))
        else:
            print(("  ✓ " if good else "  ! ") + reason)
        if good is False:
            probe_failed = reason
            # 默认重填：探测已给出可读原因（key 无效/额度不足/网络不通），
            # 新手在"回车即保存"的默认下极易把坏配置带进启动流程。
            again = _ask(_t("回车=重新填写 / 输入 n = 仍然保存",
                            "Enter=re-enter / n=save anyway"), "y")
            if not str(again).strip().lower().startswith("n"):
                return run_interactive(force=True, path=target)
        if good and embedding:
            # 嵌入也要单独测：多数中转站只转对话模型，bge 一类会 404，之后每次记忆
            # 检索都失败并降级——但那时配置已保存、新人只会看到"记忆不好用"。
            print(_t(f"正在测试 Embedding {embedding['model']} …",
                     f"Probing embedding {embedding['model']} …"))
            ok_emb, why_emb = probe_endpoint(base_url, api_key, embedding["model"])
            if ok_emb is False:
                print("  ! " + why_emb)
                print(_t("    （该端点不提供这个 embedding 模型时，记忆检索会每次失败并降级）",
                         "    (if this endpoint has no such embedding model, memory "
                         "recall fails and degrades on every call)"))
                pick = _ask(_t("1) 仍按此保存   2) 跳过 embedding（记忆检索降级为关键词兜底）",
                               "1) save anyway   2) skip embedding (keyword fallback)"), "2")
                if not str(pick).strip().startswith("1"):
                    embedding = None
                    print(_t("  - 已跳过 embedding（之后可在网页「设置」页补配）",
                             "  - embedding skipped (set it later on the Settings page)"))

    # 6) 写盘（基底是已有配置：只改引导问到的字段，不动用户设过的其它项）
    cfg = build_config(_base_config(target), base_url=base_url, api_key=api_key,
                       model=model, embedding=embedding)
    written = write_config(cfg, target)
    print()
    print(_t(f"已写入 {written.name}：base_url={base_url} model={model} api_key={mask(api_key)}",
             f"Wrote {written.name}: base_url={base_url} model={model} api_key={mask(api_key)}"))
    if embedding is None:
        print(_t("提示：未配置 Embedding，记忆检索会降级为关键词兜底（不影响任务执行）。",
                 "Note: no Embedding configured; memory recall degrades to keyword search."))
    if probe_failed:
        # "仍然保存"等于把一份已知不可用的配置带进启动流程：明说后果与修法，
        # 否则新人会以为启动成功就万事大吉，直到第一次提问才失败。
        print()
        print("  ! " + _t("端点没通过探测，这份配置**现在还不能用**：启动后每次模型调用都会失败。",
                         "The endpoint failed the probe; this config **cannot work yet** — "
                         "every model call will fail after startup."))
        print(_t("    修法：启动后打开网页 →「设置」→ 把 Base URL / 模型名 / key 改成同一家的，"
                 "点「测试连通性」看到「正常」再开始研究。",
                 "    Fix: after startup open the web UI → Settings → set base_url / model / key "
                 "from the same provider, run \"test connectivity\" until it is OK."))
    print(_t("下一步：启动后打开 http://localhost:8080 ，首次登录点「创建管理员」。",
             "Next: after startup open http://localhost:8080 and create the admin account."))
    return 0


def probe_configured(path: Path | None = None) -> int:
    """复查已写入配置的端点连通性（不提问）：装好依赖后由启动脚本调用。

    返回 0 = 连通 / 跳过（回环、内网按策略不探测）；1 = 不连通或配置不完整。
    """
    p = Path(path or CONFIG_PATH)
    ok, why = config_status(p)
    if not ok:
        print(_t(f"跳过连通性复查：{why}", f"Skip probe: {why}"))
        return 0          # 配置未完成是 [2/6] 的事，这里不重复报错
    cfg = json.loads(p.read_text(encoding="utf-8"))
    llm = cfg.get("llm") or {}
    base_url = str(llm.get("base_url") or "")
    allowed, why_skip = probe_allowed(base_url)
    if not allowed:
        print("  - " + why_skip)
        return 0
    print(_t(f"连通性复查 {base_url} …", f"Re-checking {base_url} …"))
    good, reason = probe_endpoint(base_url, str(llm.get("api_key") or ""),
                                  str(llm.get("model") or ""))
    if good is None:
        print("  - " + reason)
        return 0
    print(("  ✓ " if good else "  ! ") + reason)
    if not good:
        print(_t("    （端点不通时任务会在调用模型这一步失败；请先在网页「设置」页"
                 "把 base_url / 模型名 / key 改成同一家的）",
                 "    (with a dead endpoint every task fails at the model call; fix "
                 "base_url / model / key on the Settings page first)"))
    return 0 if good else 1


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
    if "--probe" in args:
        return probe_configured()
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
