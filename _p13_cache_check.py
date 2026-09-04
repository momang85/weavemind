# -*- coding: utf-8 -*-
"""P1-3 验证：结果缓存键必须包含 project/user/context，避免跨项目/跨用户命中。"""
import os, sqlite3, tempfile, sys

import web_ui

# 用项目本地临时目录隔离 DB（%TEMP%\dsh-* 对 sqlite 不可写，见环境说明）
_proj_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_p13tmp")
os.makedirs(_proj_tmp, exist_ok=True)
web_ui.DB_PATH = os.path.join(_proj_tmp, "tasks.db")

# 等价于 _init_db 的建表+补列逻辑
db = sqlite3.connect(web_ui.DB_PATH, timeout=5)
db.execute("""CREATE TABLE IF NOT EXISTS task_history(
 task_id TEXT PRIMARY KEY, goal TEXT, status TEXT,
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP, report TEXT)""")
cols = [r[1] for r in db.execute("PRAGMA table_info(task_history)").fetchall()]
for col, ddl in (("conversation_id", "TEXT DEFAULT ''"),
                 ("parent_task_id", "TEXT DEFAULT ''"),
                 ("context", "TEXT DEFAULT ''"),
                 ("project", "TEXT DEFAULT 'default'"),
                 ("user", "TEXT DEFAULT ''")):
    if col not in cols:
        db.execute("ALTER TABLE task_history ADD COLUMN %s %s" % (col, ddl))
db.commit()

NOW = "datetime('now', 'localtime')"
import datetime as _dt
_ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_m = lambda n: (_dt.datetime.now() - _dt.timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M:%S")
# 用相对当前时间的完成时间，保证落在 TTL 窗口内
rows = [
    ("t-1", "写一篇报告", "SUCCESS", _ts, "REPORT_ALICE_PROJECT_A", "projA", "u-alice", "ctx-x"),
    ("t-2", "写一篇报告", "SUCCESS", _m(1), "REPORT_BOB_DEFAULT", "default", "u-bob", "ctx-x"),
    ("t-3", "写一篇报告", "SUCCESS", _m(2), "REPORT_ALICE_DEFAULT_CTXY", "default", "u-alice", "ctx-y"),
    ("t-4", "写一篇报告", "SUCCESS", _m(3), "REPORT_ALICE_DEFAULT_CTXX", "default", "u-alice", "ctx-x"),
    ("t-5", "另一个目标", "SUCCESS", _m(4), "REPORT_OTHER", "projA", "u-alice", "ctx-x"),
    ("t-6", "写一篇报告", "PENDING",  None, None, "default", "u-alice", "ctx-x"),
]
db.executemany(
    "INSERT INTO task_history(task_id,goal,status,completed_at,report,project,user,context) "
    "VALUES(?,?,?,?,?,?,?,?)", rows)
db.commit(); db.close()

ttl = 1440

# 场景1：alice/projA/ctx-x => 应命中 t-1
r = web_ui._find_cached_task("写一篇报告", ttl, "projA", "u-alice", "ctx-x")
assert r and r["task_id"] == "t-1", ("场景1失败", r)
assert r["report"] == "REPORT_ALICE_PROJECT_A", r

# 场景2：bob/default/ctx-x => 应命中 t-2（而非 alice 的 t-1/t-4）
r = web_ui._find_cached_task("写一篇报告", ttl, "default", "u-bob", "ctx-x")
assert r and r["task_id"] == "t-2", ("场景2失败", r)

# 场景3：alice/default/ctx-y => 应命中 t-3（context 隔离）
r = web_ui._find_cached_task("写一篇报告", ttl, "default", "u-alice", "ctx-y")
assert r and r["task_id"] == "t-3", ("场景3失败", r)

# 场景4：alice/default/ctx-x => 应命中 t-4（同项目同用户同context最新SUCCESS）
r = web_ui._find_cached_task("写一篇报告", ttl, "default", "u-alice", "ctx-x")
assert r and r["task_id"] == "t-4", ("场景4失败", r)

# 场景5：不同 goal => None
r = web_ui._find_cached_task("完全不同的目标", ttl, "default", "u-alice", "ctx-x")
assert r is None, ("场景5失败", r)

# 场景6：PENDING（t-6）不计入缓存
r = web_ui._find_cached_task("写一篇报告", ttl, "default", "u-alice", "ctx-x")
assert r and r["task_id"] == "t-4", ("场景6失败", r)

# 场景7：未知用户 => 无命中
r = web_ui._find_cached_task("写一篇报告", ttl, "default", "u-eve", "ctx-x")
assert r is None, ("场景7失败", r)

print("P1-3_CACHE_CHECK_ALL_PASSED")
sys.exit(0)