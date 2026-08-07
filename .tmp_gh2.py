# -*- coding: utf-8 -*-
"""完成 GitHub 仓库优化（token 从环境变量读取）。"""
import json
import os
import urllib.request

TOKEN = os.environ["GH_PAT"]
REPO = "momang85/weavemind"
API = "https://api.github.com"


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "weavemind-admin",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# 1) topics + description
patch = {
    "topics": [
        "ai-agents", "multi-agent", "llm", "agent-orchestration",
        "chromadb", "self-evolving", "local-ai", "agent-framework",
        "python", "visualization",
    ],
    "description": (
        "A visible, self-remembering, self-evolving multi-agent AI team that runs "
        "on your own computer. 看得见、会记忆、会自我进化的 AI 团队。"
    ),
    "homepage": "https://github.com/momang85/weavemind",
}
try:
    r = call("PATCH", f"/repos/{REPO}", patch)
    print("PATCH ok")
    print("topics:", r.get("topics"))
    print("description:", r.get("description"))
except Exception as e:
    print("PATCH failed:", e)

# 2) 元数据
repo = call("GET", f"/repos/{REPO}")
print("stars:", repo.get("stargazers_count"), "open_issues:", repo.get("open_issues_count"))

# 3) CI 最新
runs = call("GET", f"/repos/{REPO}/actions/runs?per_page=3")
for run in runs.get("workflow_runs", [])[:3]:
    print("CI:", run["head_sha"][:7], run["conclusion"], run["created_at"])

# 4) topics 专用端点
try:
    t = call("PUT", f"/repos/{REPO}/topics", {
        "names": [
            "ai-agents", "multi-agent", "llm", "agent-orchestration",
            "chromadb", "self-evolving", "local-ai", "agent-framework",
            "python", "visualization",
        ],
    })
    print("topics now:", t.get("names"))
except Exception as e:
    print("topics PUT failed:", e)

# 5) 创建首个 Release
try:
    rel = call("POST", f"/repos/{REPO}/releases", {
        "tag_name": "v0.1.0",
        "name": "WeaveMind v0.1.0 — 织光初版",
        "body": (
            "首个可运行版本：一支看得见、会记忆、会自我进化的 AI 团队。\n\n"
            "## 亮点\n"
            "- 10 个专职 Worker 并行 DAG 执行（搜索/抓取/摘要/代码/数据/EDA/训练/报告/打包/文件）\n"
            "- 全程可视化：实时进度、任务树、Agent 拓扑、记忆与进化回放\n"
            "- ChromaDB 长期记忆 + 策略复用\n"
            "- 计划可编辑、自主迭代、策略进化锦标赛（安全红线 + 人工审批）\n"
            "- 一键启动：Windows start.bat / bash start.sh / Docker Compose\n"
            "- 工程韧性：双源 LLM failover、搜索无果自动转直接生成、代码编译自检、Worker 守护\n\n"
            "## 安装\n"
            "```bash\n"
            "git clone https://github.com/momang85/weavemind.git && cd weavemind\n"
            "cp config.example.json config.json   # 填入 API Key\n"
            "bash start.sh                        # 或 start.bat / docker compose up --build -d\n"
            "```\n\n"
            "详细见 [README](https://github.com/momang85/weavemind)。"
        ),
        "draft": False,
        "prerelease": False,
    })
    print("release created:", rel.get("html_url"))
except Exception as e:
    print("release failed:", e)

# 6) 启用 GitHub Pages（source: GitHub Actions workflow）
try:
    pg = call("POST", f"/repos/{REPO}/pages", {"build_type": "workflow"})
    print("pages enabled:", pg.get("html_url"), pg.get("status"))
except Exception as e:
    print("pages failed:", e)
