# -*- coding: utf-8 -*-
"""生成 config.json users 段的密码哈希（与 web_ui.py 的格式保持一致）。

用法：
    python scripts/hash_password.py admin 你的密码
    python scripts/hash_password.py viewer 你的密码 --role viewer

输出可直接合并进 config.json 的 users 段。也可以不手工配置：
设置环境变量 WEAVEMIND_ADMIN_PASSWORD 后启动，系统会自动创建 admin。
"""
import argparse
import base64
import hashlib
import json
import secrets


def hash_password(password: str, iterations: int = 200000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"pbkdf2_sha256${iterations}"
        f"${base64.b64encode(salt).decode('ascii')}"
        f"${base64.b64encode(dk).decode('ascii')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 WeaveMind 用户密码哈希")
    parser.add_argument("username", help="用户名")
    parser.add_argument("password", help="密码（不会写入输出以外的地方）")
    parser.add_argument("--role", default="admin", choices=["admin", "viewer"],
                        help="角色：admin 或 viewer")
    parser.add_argument("--iterations", type=int, default=200000,
                        help="PBKDF2 迭代次数（默认 200000）")
    args = parser.parse_args()
    entry = {
        "password_hash": hash_password(args.password, args.iterations),
        "role": args.role,
    }
    print(json.dumps(
        {"users": {args.username: entry}},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
