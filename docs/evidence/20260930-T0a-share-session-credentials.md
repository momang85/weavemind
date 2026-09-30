# T0-a 分享与会话凭据（服务端可验证 + 持久撤销）

批次：阶段T `T0-a`（09-30 晚扩大审查 F01/F02）。基线 `8fff008`。
范围：`web_ui.py` 分享/会话凭据路径 + 新接缝 `auth_credentials.py`。
**未改**用户模板/模型/代理/权限/真实口令与人审；**未发**外部网络请求；
验收只用**合成账户 + 临时目录隔离库**。

## 1. 反例（先复现，再修）

| 编号 | 主审反例（原话） | 本批独立复现读数 |
|---|---|---|
| F01-a | 无口令验证，自造常量分享 Cookie 从拒绝变放行 | 带口令分享：`GET /share/<tok>` 与 `GET /files/<tid>/charts/a.png` 带 `Cookie: share_<tok>=ok` **旧=200** |
| F01-b | 分享凭据是常量 | 口令验证通过的 `Set-Cookie` **旧=`share_<tok>=ok`**（客户端可自造） |
| F01-c | 改密/撤销不影响已下发凭据 | 改分享口令后旧 Cookie **旧仍 200** |
| F02-a | 登出仅删内存，重启会话从 DB 复活 | 登出 → 清内存 → `_load_sessions()` → **旧会话复活，`/api/status` 200** |
| F02-b | 删除用户后旧 admin token 仍有效 | 删 `bob` 后其旧 token `GET /api/users` **旧=200** |
| F02-c | 降权后旧 admin 会话仍有效 | 降 `carol` 为 viewer 后其旧 token `GET /api/users` **旧=200** |
| F02-d | 改密不使旧会话失效 | 改口令后两条旧会话 **旧均 200** |

失败→通过用例都在 `test_auth_audit.py` 的 `T0-a` 区块（8 条）：
**不带修复时 7 失败 + 1 报错（缺 `share_grants` 表），修复后 27/27 通过**。

**复现方法学纠错（重要）**：这批用例第一版**全绿**——因为测试库没有 `sessions`
表，`INSERT OR REPLACE INTO sessions` 静默失败、会话只剩内存，"登出复活"被掩盖。
用例现在按生产启动顺序先 `_init_db()` 再登录（`_seed_users_and_db`），
反例才如实失败。**假绿比漏测更危险**。

## 2. 根因（行号为修复前）

1. `web_ui.py:2339` 分享放行判据是**字面字符串比较** `share_<token>=ok`；
   `3575` 只下发这个常量。任何知道分享 token 的人自造 Cookie 即可绕过口令。
2. `web_ui.py:883` `_delete_session` 只 `_sessions.pop()`，**不删库行**；
   `816` `_load_sessions()` 启动时按库行复活 → 登出不是撤销。
3. `web_ui.py:864` `_get_session` 只读进程内字典，**角色取签发时的缓存**；
   删号/降权/改密都不触碰会话 → 旧 token 照用。
4. `web_ui.py:7686/7734` 用户管理只改 `config.json`，没有任何会话撤销语义。

## 3. 最小修复（接缝先立）

新增 `auth_credentials.py`（窄接缝，只依赖标准库 + 一个 SQLite 路径，**不 import
web_ui**，可用隔离库直接验收）：

- 表：`sessions`（补 `gen`/`issued` 列，老库就地升级）、`auth_principal`（主体代次）、
  `share_grants`（**只存 SHA-256**，绑定 `share_id`/代次/到期）。
- `verify_session_row()`：一次查询拿到"会话行 + 账户当前代次"（每请求一次真源读）。
- `issue/verify/revoke_share_grant()`：签发随机凭据；校验存在、未过期、属该分享、代次未变。
- `revoke_account()`：一次事务内"代次 +1 且删该账户全部会话行"。

`web_ui.py` 改法：

- `_share_cookie_ok`（web_ui.py:2418）：凭据必须**服务端签发**且能用哈希核验；
  显式拒绝常量 `ok`；核验异常一律拒绝（fail closed）。
- `_share_access_ok`（:2446）：分享不存在/已过期即拒；无口令分享保持原公开语义（没有可验证的秘密）。
- `_share_access_for_task_ok`（:2488）：附件按"该任务任一分享的有效凭据"判断 →
  **正文与附件共用同一授权**。
- `_share_grants_ttl`（:2464）：凭据有效期不超过分享剩余有效期。
- `_handle_share_auth`：口令通过后签发随机凭据；**签发失败返回 503 且不放行**。
- `_generate_share_token` / `_revoke_share_token`：口令变化或撤销分享时**递增分享代次并撤销已下发凭据**。
- `_get_session`（:899）：**每次都回真源核**（内存缓存不作为放行依据），
  角色按**当前** `config.json` 重取，代次不符即撤销；真源不可读 → 拒绝。
- `_revoke_account_sessions`（:940）：改密/改角色/删号后调用；`_post_users`/`_delete_users` 接入。
- 登录/初始化管理员：会话无法持久化就**拒绝签发**（503），不再发"重启即复活"的内存会话。

## 4. 隔离双实例验收（39/39）

`scripts/t0a_credential_isolation_check.py` → 证据 `docs/evidence/t0a_credential_isolation.json`。
**两个（第三个按需新起）真实 `web_ui.Handler` 进程**，共享同一隔离
`agents.db`/`config.json`/`share_links.json`（`WEAVEMIND_DB` + `WEAVEMIND_DATA_DIR` 指向临时目录），
只走 127.0.0.1 真实 HTTP，全部合成账户：

| 场景 | 读数 |
|---|---|
| S1 跨实例会话可用（排除"全拒"假通过） | A 登录的会话，B `/api/status` **200** |
| S2 登出持久生效 | A 登出后：同实例 **401**、另一实例 **401**、登出后**新起实例 C 也 401** |
| S3 自造常量 Cookie | 分享页 **401**、附件 **401** |
| S4 服务端凭据跨实例可用 | B 口令验证 **302**，凭据长度 **43**（≠`ok`）；B 看正文 **200**、取附件 **200** |
| S5 凭据只对该分享有效 | 同一凭据换另一分享 **401** |
| S6 改密即失效 | A 改分享口令 → B 旧凭据 **401**；新口令换新凭据 **200** |
| S7 删号 | bob 旧 token 读管理接口 **200→401**、写 **401**、新起实例 C 也 **401** |
| S8 降权 | carol 旧 admin 会话读/写 **200→(401/403)**，新起实例同样拒绝 |
| S9 改密 | 另一实例旧会话 **401**；旧口令登录 **401**、新口令 **200** |

### 双实例比单进程多抓到的残余（已修）

单进程用例是"清内存后重载"，掩盖了一条真实漏洞：**A 登出后 B 仍 200**。
根因是 B 早已把会话缓存进内存，而当时 `_get_session` 只在**缓存未命中**时才查库。
改为**每次回真源核**后 S2 通过。这条只有隔离双实例能看见——单进程测不出来。

### 探针假象（如实记录，不是产品缺陷）

分享口令验证是 `302 + Set-Cookie`；`urllib` 默认**跟跳转且丢掉 Cookie**，
于是探针拿到"跳过去以后没凭据"的 401。已改为不跟跳转（`_NoRedirect`）后才读到真实的 302。

## 5. 定向回归（逐个文件，真实退出码）

`test_auth_audit.py` 27 OK（新增 9 条，其中 8 条反例 + 1 条旧库升级）、
`test_delivery_chain.py` 408 OK、`test_p0.py` OK、`test_deploy_manifest.py` 40 OK、
`test_review_edit_api.py` OK(skipped=3)、`test_frontend_guards.py` OK、
`test_task_persistence.py` OK、`test_startup_readiness.py` OK、`test_offline_delivery.py` OK。

**必要回归改动（说明是规格变化，不是放松断言）**：`test_delivery_chain.py`
`test_password_share_flow` 原断言 `share_<token>=ok` 这一常量；按 T0-a 新规格改为
"凭据来自 Set-Cookie 且 ≠`ok`、长度 ≥24、能放行正文与附件"，并**新增**"自造 `=ok` 被拒"。
断言净增强。

**升级路径**：老库（`sessions` 无 `gen`/`issued`）就地补列，未过期旧会话仍有效
（不会因升级把所有人踢下线），且此后同样受代次约束。用例
`test_t0a_legacy_session_table_upgrades_without_mass_logout`。

## 6. 真实库就地升级与上线读数

- `launcher.py restart`（一次成功，租约等待修复仍在）→ **16/16 服务、HTTP 200 @ 8080、研究能力就绪**。
- 真实 `agents.db` 的 `sessions` 表升级后列 = `[token, user, role, expires, gen, issued]`，
  **既有 2 条会话行被保留**（`auth_principal` 为 0 → 尚无代次递增），即升级**没有强制任何人重新登录**。
- 远端 CI：`d325cc4` 的 **CI success**（run `36721205633`）、Pages success（run `36721205664`）。

## 7. 未验 / 残留（不假装）

- **真实浏览器跨设备**：双实例是同一台机器的两个进程；跨机器/跨域的 Cookie 属性
  （`Secure` 仅在反代 https 下追加）未实机验证。
- **生产反代下的 `X-Forwarded-For`**：暴力破解计数与登录锁定仍按该头取 IP，未在本批改动。
- **运行中实例**：本批不重启线上服务（避免动当前权限/会话）；修复在下次重启后生效，
  升级不强制任何人重新登录。
- 既有非本批项：`/api/status` 在处理中留下未关闭的 sqlite 连接（`web_ui.py:1173`，ResourceWarning），
  与本批无关，未顺手改。
- 生产是否已被利用**未知**；本批只关闭机制漏洞，不主张发生过泄露。
