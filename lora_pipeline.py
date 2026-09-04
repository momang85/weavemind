# -*- coding: utf-8 -*-
"""LoRA 蒸馏→训练→部署→评测统一管线入口（架构约束④自动化）。

把 docs/LLM模式与LoRA管理.md「新增 Worker LoRA 流程」的 6 步手工操作
收敛为一条命令：

    python lora_pipeline.py --worker content_summary

阶段（可用 --skip-* 跳过）：
1. 蒸馏   ：子进程跑 distill_v2.py（教师=智谱，产出 distill_data_v2.jsonl + test）
2. 训练   ：子进程跑 finetune_qlora.py --data distill_data_v2.jsonl --out loras/<worker>
3. 净化   ：删除训练残留（optimizer/rng/scaler/scheduler/trainer_state/training_args），
            校验推理必需文件（adapter_config.json + adapter_model.*）
4. 注册   ：更新 lora_servers.json（端口继承预留或自动分配；先 enabled=false 灰度）
5. 重启   ：找到并重启 lora_serve 进程（找不到则提示手工启动）
6. 评测门 ：eval_distill.py --compare --gate，质量回退（>10pp）→ 退出码 1 + 回退 cloud

示例：
  python lora_pipeline.py --worker content_summary              # 全流程
  python lora_pipeline.py --worker content_summary --skip-train # 只蒸馏+注册+评测
  python lora_pipeline.py --worker ranking --dry-run            # 只打印计划
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

logger = None  # 无 logging 依赖，管线以 print 输出为主（脚本式工具）

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("WM_LORA_CONFIG", "lora_servers.json")
DEFAULT_PORT_BASE = 8765
# 训练残留：部署目录净化时删除（推理不需要）
TRAINING_ARTIFACTS = (
    "optimizer.pt", "rng_state.pth", "scaler.pt", "scheduler.pt",
    "trainer_state.json", "training_args.bin", "adapter_model.bin",
    "global_step", "checkpoint-*",
)
# 推理必需：净化后必须存在
REQUIRED_INFERENCE = ("adapter_config.json",)

# 端口分配逻辑：先继承 _future_<worker> 预留端口，否则从 DEFAULT_PORT_BASE 起找空闲
_FUTURE_PREFIX = "_future_"


# ─────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────

def _run(cmd: list[str], dry_run: bool = False) -> int:
    """执行子进程命令；dry_run 只打印。返回退出码。"""
    print(f"\n$ {' '.join(cmd)}", flush=True)
    if dry_run:
        return 0
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    return proc.returncode


def _load_config() -> dict:
    try:
        with open(os.path.join(REPO_ROOT, CONFIG_FILE), encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and isinstance(cfg.get("servers"), dict):
            return cfg
    except Exception:
        pass
    return {"servers": {}}


def _save_config(cfg: dict) -> None:
    with open(os.path.join(REPO_ROOT, CONFIG_FILE), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _find_lora_serve_pid() -> int | None:
    """找正在运行的 lora_serve.py 进程 PID。

    Windows：tasklist 无法区分 python 脚本，返回 None（由端口探测提示代替）。
    """
    if os.name == "nt":
        return None
    try:
        out = subprocess.run(
            ["pgrep", "-f", "lora_serve.py"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            return False


def _alloc_port(cfg: dict, worker: str) -> int:
    """端口分配：继承 _future_<worker> 预留 → 否则从 8765 起找未占用。"""
    servers = cfg.get("servers") or {}
    future = servers.get(f"{_FUTURE_PREFIX}{worker}")
    if isinstance(future, dict) and future.get("port"):
        return int(future["port"])
    used = {
        int(s["port"]) for s in servers.values()
        if isinstance(s, dict) and s.get("port")
    }
    port = DEFAULT_PORT_BASE
    while port in used or _port_in_use(port):
        port += 1
    return port


# ─────────────────────────────────────────────
# 阶段 3：部署目录净化
# ─────────────────────────────────────────────

def purge_training_artifacts(worker: str) -> list[str]:
    """删除训练残留，返回删除清单。校验推理必需文件存在（缺失则抛错）。"""
    import glob
    lora_dir = os.path.join(REPO_ROOT, "loras", worker)
    if not os.path.isdir(lora_dir):
        raise FileNotFoundError(f"LoRA 目录不存在: {lora_dir}（先跑训练阶段）")
    removed: list[str] = []
    for pat in TRAINING_ARTIFACTS:
        for fp in glob.glob(os.path.join(lora_dir, pat)):
            try:
                os.remove(fp)
                removed.append(os.path.basename(fp))
            except Exception as exc:
                print(f"  ⚠️ 删除失败 {fp}: {exc}")
    missing = [r for r in REQUIRED_INFERENCE
               if not os.path.exists(os.path.join(lora_dir, r))]
    if missing:
        raise FileNotFoundError(
            f"推理必需文件缺失 {missing}（{lora_dir} 不是合格部署目录）"
        )
    return removed


# ─────────────────────────────────────────────
# 阶段 4：注册 lora_servers.json
# ─────────────────────────────────────────────

def register_server(worker: str, enabled: bool = False) -> dict:
    """注册/更新 worker 条目。返回该条目。

    - 已存在（含 _future_ 预留）→ 保留 system_prompt/描述，更新 port/lora_path/enabled；
    - 不存在 → 新建（端口自动分配，先 enabled=false 灰度）；
    - _future_<worker> 条目自动转为正式条目。
    """
    cfg = _load_config()
    servers = cfg.setdefault("servers", {})
    future_key = f"{_FUTURE_PREFIX}{worker}"
    future = servers.pop(future_key, None) if future_key in servers else None
    existing = servers.get(worker)
    entry = dict(existing) if isinstance(existing, dict) else {}
    if not entry:
        entry = {
            "port": _alloc_port(cfg, worker),
            "lora_path": f"loras/{worker}",
            "description": f"{worker} Worker LoRA（lora_pipeline 自动注册）",
        }
    if future and isinstance(future, dict):
        entry.setdefault("system_prompt", future.get("system_prompt", ""))
        entry.setdefault("description", future.get("description", ""))
    entry["lora_path"] = f"loras/{worker}"
    entry["enabled"] = bool(enabled)
    servers[worker] = entry
    _save_config(cfg)
    return entry


# ─────────────────────────────────────────────
# 阶段 6：评测门（eval_distill --compare --gate）
# ─────────────────────────────────────────────

def run_eval_gate(worker: str, limit: int = 5, dry_run: bool = False) -> int:
    """质量门禁：同批对比 + 回退判定。返回 eval_distill 退出码（0=达标）。"""
    return _run([
        sys.executable, "eval_distill.py", "--compare", "--gate",
        "--limit", str(limit), "--worker", worker,
    ], dry_run=dry_run)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LoRA 蒸馏→训练→部署→评测统一管线")
    ap.add_argument("--worker", required=True, help="Worker 名（如 content_summary）")
    ap.add_argument("--epochs", type=int, default=3, help="训练轮数（默认 3）")
    ap.add_argument("--limit", type=int, default=5, help="评测样本数（默认 5）")
    ap.add_argument("--skip-distill", action="store_true", help="跳过蒸馏（复用已有数据）")
    ap.add_argument("--skip-train", action="store_true", help="跳过训练（复用已有 LoRA）")
    ap.add_argument("--skip-purge", action="store_true", help="跳过部署目录净化")
    ap.add_argument("--skip-register", action="store_true", help="跳过 lora_servers.json 注册")
    ap.add_argument("--skip-restart", action="store_true", help="跳过 lora_serve 重启")
    ap.add_argument("--skip-eval", action="store_true", help="跳过评测门")
    ap.add_argument("--enable", action="store_true",
                    help="注册时直接 enabled=true（默认灰度 false，评测通过才建议启用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    args = ap.parse_args()

    worker = args.worker
    dry = args.dry_run
    print(f"═══ LoRA 管线: {worker}（dry-run={dry}）═══")

    # ── 1. 蒸馏（distill_v2 按 worker 参数化输出文件） ──
    if not args.skip_distill:
        rc = _run([sys.executable, "distill_v2.py", "--worker", worker], dry_run=dry)
        if rc != 0:
            print("❌ 蒸馏失败，中止", flush=True)
            return rc
    from distill_v2 import _data_files
    raw_data, train_data, test_data = _data_files(worker)
    if not os.path.exists(train_data) or os.path.getsize(train_data) == 0:
        if dry:
            print(f"⚠️ 训练数据 {train_data} 尚不存在（dry-run 不实际执行蒸馏），按计划继续", flush=True)
            n = 0
        else:
            print(f"❌ 训练数据缺失: {train_data}（--skip-distill 需已有数据）", flush=True)
            return 1
    else:
        n = sum(1 for _ in open(train_data, encoding="utf-8") if _.strip())
    print(f"训练数据: {n} 条（{train_data}）", flush=True)

    # ── 2. 训练 ──
    lora_out = os.path.join("loras", worker)
    if not args.skip_train:
        rc = _run([
            sys.executable, "finetune_qlora.py",
            "--data", os.path.basename(train_data),
            "--out", lora_out,
            "--epochs", str(args.epochs),
        ], dry_run=dry)
        if rc != 0:
            print("❌ 训练失败，中止", flush=True)
            return rc
    if dry or args.skip_train:
        if not os.path.isdir(os.path.join(REPO_ROOT, lora_out)):
            print(f"⚠️ 未找到 {lora_out}（--skip-train 需已有 LoRA）", flush=True)

    # ── 3. 净化 ──
    if not args.skip_purge and not dry:
        try:
            removed = purge_training_artifacts(worker)
            print(f"净化: 删除 {len(removed)} 个训练残留" + (f" {removed}" if removed else ""), flush=True)
        except FileNotFoundError as exc:
            print(f"⚠️ 净化跳过: {exc}", flush=True)
    elif not args.skip_purge:
        print(f"净化: [dry-run] 将删除 {lora_out} 下的训练残留", flush=True)

    # ── 4. 注册 ──
    if not args.skip_register and not dry:
        entry = register_server(worker, enabled=args.enable)
        print(f"注册: {worker} → port {entry['port']} / {entry['lora_path']} / "
              f"enabled={entry['enabled']}", flush=True)
        if _port_in_use(int(entry["port"])):
            print(f"⚠️ 端口 {entry['port']} 已被占用（旧服务未停？）", flush=True)
    elif not args.skip_register:
        print(f"注册: [dry-run] 将写入 lora_servers.json（enabled={args.enable}）", flush=True)

    # ── 5. 重启 lora_serve ──
    if not args.skip_restart:
        pid = _find_lora_serve_pid()
        if pid:
            print(f"lora_serve 运行中 (pid={pid})，请手工重启以加载新 adapter", flush=True)
        elif dry:
            print("lora_serve 未运行（dry-run 不启动）", flush=True)
        else:
            print("lora_serve 未运行，启动中...", flush=True)
            rc = _run([sys.executable, "lora_serve.py"], dry_run=False)
            if rc != 0:
                print("⚠️ lora_serve 启动失败，请手工启动", flush=True)

    # ── 6. 评测门 ──
    if not args.skip_eval and not dry:
        rc = run_eval_gate(worker, limit=args.limit, dry_run=False)
        if rc != 0:
            print(f"\n❌ 质量回退（exit={rc}）：建议回退 cloud 模式", flush=True)
            return rc
        if args.enable:
            print("\n✅ 质量达标；已 enabled=true，保持 hybrid", flush=True)
        else:
            print("\n✅ 质量达标。确认无误后请手工把该 worker 的 enabled 改为 true", flush=True)
    elif not args.skip_eval:
        print(f"评测门: [dry-run] 将运行 eval_distill.py --compare --gate --worker {worker}", flush=True)

    print(f"\n═══ 管线完成: {worker} ═══", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
