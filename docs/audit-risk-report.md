# 织光（WeaveMind）风险审查报告

> 首轮人工核验发现（主审查员逐行核对，含文件:行号）。子代理并行审计结论合并后更新。
> 严重级：P0 高危 / P1 中危 / P2 低危 / P3 隐患。
> 说明：`config.json`、`.env` 均未入库（已用 `git ls-files` / `git log --all` 验证，无真实密钥进 git 历史）；本报告不包含任何在库密钥。

---

## 1. 已确认的关键问题（主审查员人工核对）

### P0-1  `/files/<tid>/<rel>` 端点可越权读取任务整个工作区，远超"分享报告"范围
- 位置：`web_ui.py` `do_GET` `/files/` 分支（约 2236–2263）；`_safe_workspace_path`（1023–1030）。
- 触发：分享链接持有者（匿名）请求 `/files/<task_id>/data/xxx.csv` 或 `/files/<task_id>/charts/x.png`、`/files/<task_id>/reports/...`、`/files/<task_id>/xxx.zip`；登录的 viewer 可在任意任务上（无需分享）请求 `/files/<tid>/...`。
- 根因：文件服务先解析 `/files/<task_id>/<rel>`，`_safe_project_path` 限定在 `project/`，但失败后**回退 `_safe_workspace_path(rel, tid)`** 把解析范围放大到整个任务工作区根目录（`project/data/charts/reports/*.zip` 全在 `task_workspace(tid)` 之下）。分享权限本应只暴露报告与该报告引用的图片。
- 影响：分享一个报告的链接，等于把该任务的全部原始数据、图表、中间产物、交付包一起对外公开；viewer 角色还能读任意未分享任务的工作区文件。**机密性越界。**
- 建议：文件服务收口到"该任务 `reports/`、`charts/`、`project/` 中**被报告引用**的白名单路径"，或至少去掉 `_safe_workspace_path` 全工作区回退、只允许 `project/` 与报告引用的图表文件。

### P0-2  `render_charts.py` / 游戏脚本运行路径绕过密钥剥离沙箱（凭据泄露面）
- 位置：`orchestrator_v2.py` 4485–4513（`render_charts.py` 用 `subprocess.run([sys.executable, script])` 且**未传剥离密钥的 env**，继承进程环境）；4906–4924（游戏/代码片段 `env = dict(os.environ)` 仅加 `SDL_VIDEODRIVER`，未剥离）。
- 触发：LLM 生成的图表/游戏 Python 脚本内 `os.environ` 读取；脚本走 `--network`（restricted 模式下子进程保留机器出网能力）。
- 根因：编排器内联执行生成代码时未走 `code_sandbox.run_script` / `sanitize_env`，与 `code_execution_worker` 的密钥剥离承诺不一致。
- 影响：LLM 生成代码可读到进程内 `API_KEY` 类环境变量并外传 → 密钥泄露；也绕过了 `code_sandbox` 的统一降级/隔离。
- 建议：统一改用 `code_sandbox.run_script`（或至少 `sanitize_env`）。

### P1-1  登录 & 分享密码& 无速率限制（0.0.0.0 暴露下的爆破面）
- 位置：`web_ui.py` `_handle_login`（2016）、`_handle_share_auth`（2090）；服务器 `ThreadingHTTPServer(("0.0.0.0", PORT))`（3088）。
- 触发：管理员/分享密码可被远程暴力破解。`RateLimiter` 只用于 `/task` 提交且**回环地址豁免**。
- 影响：admin 口令与分享口令漫爆破；无锁定。
- 建议：对 `/api/login`、`/share/<token>/auth` 加进程内限流+失败锁定；默认只绑 127.0.0.1 或用反向代理。

### P1-2  Docker 部署 Redis 未认证且对外暴露
- 位置：`docker-compose.yml` `ports: "6379:6379"`，`command: redis-server --appendonly yes`（无 `requirepass`）；客户端 `common.py` / `web_ui.py` 连接不带密码。
- 影响：可到达部署主机的攻击者可直连 Redis，读写任务队列/结果/状态（数据面泄露与篡改）。
- 建议：Redis 不对外映射端口（改为只在 compose 内部网络），或 `requirepass` 并通过环境变量注入。

### P1-3  结果缓存按"目标文本"跨项目/跨用户/跨语言命中（缓存串台）
- 位置：`web_ui.py` `_find_cached_task(g, ttl)`（1783–1796）、`/task` 命中分支（2707–2717）。
- 触发：不同 `project`、不同用户、不同 `language`、不同 `context`，只要 `goal` 字符串完全一致且在 TTL 内，就返回别人/别项目上次的报告；且缓存命中不创建本次会话记录。
- 影响：跨项目/跨用户数据串台（机密内容被错投），会话连续性破坏。
- 建议：缓存键应含 `project`（及可选 `language`/用户作用域），命中也应登记本次 conversation。

### P2-1  分享/会话 Cookie 缺 `Secure`/`SameSite` 属性
- 位置：`web_ui.py` 2050 / 2087 / 2127 / 2535。
- 影响：HTTPS 部署下 cookie 可被明文传输窃取；跨站请求是否带 cookie 未用 SameSite 收紧。
- 建议：根据 `X-Forwarded-Proto`/请求是否为 https 追加 `Secure`，登录会话加 `SameSite=Lax`。分享放行 cookie 建议加 `Path=/share/<token>` 细化作用域。

### P2-2  分享链接 Host/Proto 头污染（链接投毒）
- 位置：`web_ui.py` `_share_link`（1930–1934）直接用请求 `Host` 与 `X-Forwarded-Proto` 拼接分享链接。
- 触发：带恶意 `Host` 头的请求生成/查询分享时，返回可被投毒的 URL 并进入通知/邮件。
- 建议：锁定合成链接用的域名（配置 `PUBLIC_BASE_URL`），不信 Host。

### P2-3  LoRA `/generate` 端无鉴权（仅绑 127.0.0.1，本地进程/恶意网页可触发推理）
- 位置：`lora_serve.py` HTTP handler（247–270）。
- 影响：本机恶意进程可无鉴权调用 GPU 推理；浏览器对 localhost:8765 的 POST 也可能被触发（消耗 GPU）。
- 建议：加共享 token；请求体未含 token 拒绝。

### P2-4  `lora_serve` 端口→adapter/名字映射不一致
- 位置：`lora_serve.py` 132、258：`_adapter_names.get(port)` 回退到首 adapter，但 `_server_names.get(port,"default")` 命名却变 default，语义漂移。
- 影响：多实例下 fallback 行为的 server 名不一致（小 bug）。

### P2-5  `/api/deliverable/run` 直接 `subprocess.Popen` 跑用户 .py，未经沙箱
- 位置：`web_ui.py` 2599–2640。
- 影响：管理员"运行交付物"路径绕过了统一下发的代码沙箱（仅手动剥 env）。
- 建议：改为复用 `code_sandbox`。

### P3-1  前端鉴权 token 存 `localStorage`（XSS 可窃取面）
- 位置：`frontend/src/auth.ts` 27。
- 影响：若前端存在任意 XSS 可窃取会话。当前前端未见 `dangerouslySetInnerHTML`、react-markdown 默认安全，暂属隐患。
- 建议：接入 CSP；敏感操作改 HttpOnly cookie 优先。

### P3-2  `_share_cookie_ok` 子串匹配存在理论误判边界
- 位置：`web_ui.py` 1573–1577。
- 影响：理论上若 token 互为前缀且 cookie 值排布恰好满足 `share_<tok>=ok` 子串可误放行；实际 token 固定长度、风险低。建议改为严格按 `;` 切分后精确比较键值。

---

## 2. 补充发现（主审查员人工核对 + Codex 探路确认）

- P0-2 细化：orchestrator `_rewrite_report_links`（4727–4757）会把报告里**任何**工作区绝对路径（含 `data/`）改写成 `/files/<tid>/...`。收紧 `/files/` 白名单后需同步此函数：仅改写落在 `reports/`、`charts/`、`project/` 下的目标，`data/` 等不改写。
- P0-1 细化：旧格式 `/files/<rel>`（单段、无 tid）经 `_safe_project_path(rel, None)` 会解析到全局 `PROJECT_DIR`，viewer 登录态可读；修复时应一并关闭旧格式回退（返回 404）。
- 既有单测 `test_delivery_chain.py` 存在匹配点（2283/2284 断言 charts/ 与 data/ 都被改写、2383 断言 `_safe_workspace_path` 仍可用、2830–2890 分享页附件走 `/files/tid/charts/...`）。修复需同步调整与新政冲突的断言并保持特权/分享行为可见可用。
- Docker/前端（前述 P1-2/P2-1/P2-2/P3-1）已核验属实。

## 3. 修复实施状态

- 已确认修复方案：P0-1（/files 白名单收口）、P0-2（render_charts 走 sanitize_env/沙箱）、P1-1（登录/分享限流+0.0.0.0）、P1-2（Redis 认证/不对外）、P1-3（缓存键加 project）。
- **Codex 实施受阻（外部）**：Codex 本机已可运行（`-m deepseek-v4-flash-0731` + 全权限），但其经 CC Switch 本地代理调用远端模型持续不稳定——`gml-5.3-flash` 返回 `MODEL_NOT_AVAILABLE`；`deepseek-v4-flash-0731` 长会话中途报 `reasoning_content must be passed back`（HTTP 400）。代理模型路由为外部应用（CC Switch）控制，本会话无法修复配置（`~/.codex/config.toml` 被主机 ACL 拒绝写入）。
- 子代理并行审计：4 路因读取超大文件(工具时限/上下文)未能回传完整结论；本报告为**主审查员逐行人工核验**所得，已足够支撑修复。

---

> 下一阶段：待 Codex 代理可用后按上述方案实施并验证；或在用户许可下由主审查员直接实施 P0 补丁。