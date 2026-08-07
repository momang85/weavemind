# -*- coding: utf-8 -*-
"""用 GitHub API 检查/优化仓库（token 从环境变量读取，不落盘）。"""
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


# 1) 设置 topics + description + homepage
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
    print("topics:", r.get("topics"))
    print("description:", r.get("description"))
except Exception as e:
    print("PATCH failed:", e)

# 2) 仓库元数据
repo = call("GET", f"/repos/{REPO}")
print("\n--- repo ---")
print("stars:", repo.get("stargazers_count"), "forks:", repo.get("forks_count"),
      "watchers:", repo.get("subscribers_count"), "issues:", repo.get("open_issues_count"))
print("created_at:", repo.get("created_at"), "pushed_at:", repo.get("pushed_at"))
print("default_branch:", repo.get("default_branch"))
print("has_issues:", repo.get("has_issues"), "has_wiki:", repo.get("has_wiki"),
      "has_pages:", repo.get("has_pages"), "archived:", repo.get("archived"))

# 3) CI 最新运行
try:
    runs = call("GET", f"/repos/{REPO}/actions/runs?per_page=20")
    print("\n--- CI runs ---")
    for run in runs.get("workflow_runs", [])[:20]:
        print(run.get("head_branch"), run.get("name"), run.get("status"),
              run.get("conclusion"), run.get("created_at"))
except Exception as e:
    print("CI query failed:", e)

# 4) 最新失败 run 的 job 详情
try:
    run_id = runs["workflow_runs"][0]["id"]
    detail = call("GET", f"/repos/{REPO}/actions/runs/{run_id}")
    print("run head_sha:", detail.get("head_sha"), "event:", detail.get("event"))
    jobs = call("GET", f"/repos/{REPO}/actions/runs/{run_id}/jobs")
    print("\n--- jobs (run", run_id, ") ---")
    for job in jobs.get("jobs", []):
        print(job.get("name"), job.get("conclusion"))
        for step in job.get("steps", []):
            if step.get("conclusion") in ("failure", "cancelled"):
                print("  FAILED STEP:", step.get("name"))
except Exception as e:
    print("job query failed:", e)

# 5) check-run annotations（失败步骤的结构化报错）
try:
    checks = call("GET", f"/repos/{REPO}/commits/{detail.get('head_sha')}/check-runs")
    print("\n--- check runs ---")
    for c in checks.get("check_runs", []):
        print(c.get("name"), c.get("conclusion"))
        out = c.get("output", {})
        print("  summary:", str(out.get("summary"))[:800].replace("\n", " | "))
        print("  text:", str(out.get("text"))[:1200].replace("\n", " | "))
        for a in c.get("output", {}).get("annotations", [])[:10]:
            print("  ANNOTATION:", a.get("path"), a.get("start_line"),
                  a.get("message")[:500].replace("\n", " | "))
except Exception as e:
    print("check-runs failed:", e)

# 6) 尝试 run 级日志 zip
try:
    req = urllib.request.Request(
        API + f"/repos/{REPO}/actions/runs/{run_id}/logs",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": "weavemind-admin",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as rr:
        data = rr.read()
    print("\nrun logs zip bytes:", len(data))
    import io, zipfile
    zf = zipfile.ZipFile(io.BytesIO(data))
    for name in zf.namelist():
        print("log file:", name)
        if "compile" in name.lower():
            print("---", name)
            print(zf.read(name).decode("utf-8", errors="replace")[-2500:])
except Exception as e:
    print("run logs failed:", e)
