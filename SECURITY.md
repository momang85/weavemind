# 安全说明 Security

## 密钥与数据

- 真实 API Key 只放在本地 `config.json` / `.env`（均已 `.gitignore`），**不要提交到仓库**；
- 数据库、记忆目录、日志同样不入库（`*.db*`、`chroma_memory*`、`logs/`）；
- Docker 方式通过 `.env` 注入密钥，密钥不写入镜像。

## 代码执行沙箱

- `code_execution` Worker 执行前会剥离 `LLM_*`、`API_KEY`、`TOKEN`、`SECRET` 等环境变量；
- 生成脚本带编译自检、超时（120s）与失败即报错；
- 建议在**可信环境**使用：生成的代码来自 LLM，本质上等于执行外部代码，
  生产部署时请接入容器级隔离（路线图中）。

## 报告漏洞

如发现安全问题，请**不要公开提 issue**，直接发送邮件到仓库主页的联系邮箱，
或通过 GitHub 的 [Security Advisories](https://github.com/momang85/weavemind/security/advisories) 私密报告。
