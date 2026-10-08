# 界面：真实阻塞与恢复条件（本批）

## 现状（照实）

- 应用**可运行**：`launcher.py up` 曾启动 16/16 服务、`http://localhost:8080` HTTP 200
  （上一批 10-08b 的启动记录仍在 `docs/evidence/20261008-resilience-cash-pressure/ui_blocker.json`）。
- 本批新增页面已进构建产物：`frontend/dist/assets/ResearchStudy-COoVdPMw.js`（19,432 B）、
  路由 `/research-study`、侧栏「本地研究试验」、演示模式拦截前缀 `/api/research-study`。
- 但工作台**需要管理员登录**：页面快照显示用户名 `admin` + 空密码框 + 登录按钮；
  `README.md` 明确「没有固定默认管理员密码」，`config.json`/`.env` 属密钥/凭据类文件。
- 因此本批**没有**界面点击证据：打开「本地研究试验」→ 上传/选样例 → 预览 → 勾选主体 →
  运行 → 折叠查看 → verify/replay → 假说卡，这几步都停在登录墙前。
- 代理侧**不读取配置密钥、不修改账号权限、不绕过登录**，也**不把脚本/HTTP 调用说成页面点击**。

## 恢复条件（用户一步即可解锁）

0. **服务已在本批提交后重新加载**（`launcher.py stop` + `up`，当时无进行中任务）：
   16/16 服务存活、`GET /` 200、新页 chunk `/assets/ResearchStudy-COoVdPMw.js` 200（19,432 B）。
   记录见 [app_reload.json](./app_reload.json)。所以下面这些点击**现在就能用**，只差登录。
1. 在本机浏览器登录一次工作台（或提供一个已登录会话）；
2. 侧栏点「本地研究试验」→「用内置两公司模拟样例」→「文件预览（只读）」→
   勾选 `900001.SH`、`900002.SH` → 「运行并保存」→ 展开四块（经营/事件/风险/内部附件）→
   「verify」「replay」「冻结假说卡」；
3. 期望与实际一致的三处：产物目录 `tmp/research-studies/f6469defdb1eac21`（研究ID 前 16 位）、
   `verify ok=true`、`replay` 同实质结果（本轮已用同一批端点函数离线验证，见 `entry_probe.txt`）；
4. 若要上传自己的文件：上传后逐文件选用途（财务宽表/日行情/新闻/日历/信用标签/反欺诈标签），
   缺某域就别给（不用假数据补齐）。

## 本轮已做的等价验证（不等于点击）

同一批端点函数（`web_ui._post_research_study_*`）离线跑通全链路：预览 → 未确认/错主体的反例 →
运行并保存（13 个内部附件）→ verify（12 项字节 hash）→ replay（同实质结果）→ 假说卡。
证据：[entry_probe.txt](./entry_probe.txt)。本批**不称**这是界面通过。
