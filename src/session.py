#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session.py — 统一会话配置（支持换账号）

凭据来源优先级:
  1. 环境变量   YOOC_TOKEN / YOOC_YIBAN_ID / YOOC_USER_ID
  2. 文件       session.json   (推荐, 换账号只改这里)
  3. 内置默认   (旧账号, 仅兜底)

session.json 格式:
  {
    "token":     "xxxx",
    "yiban_id":  "YOUR_YIBAN_ID",
    "user_id":   "YOUR_USER_ID",
    "group_id":  "YOUR_GROUP_ID",
    "exam_id":   "YOUR_EXAM_ID",
    "examuser_id": ""
  }

用法:
  python src/session.py show     # 查看当前生效会话(脱敏)
  python src/session.py init     # 生成 session.json 模板
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "session.json")

DEFAULTS = {
    "token": "",
    "yiban_id": "",
    "user_id": "",
    "group_id": "",
    "exam_id": "",
    "examuser_id": "",
}


def load():
    """返回会话字典。环境变量 > session.json > 默认。"""
    s = dict(DEFAULTS)
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                s.update({k: str(v) for k, v in json.load(f).items() if v is not None})
        except Exception:
            pass
    env_map = {
        "token": "YOOC_TOKEN",
        "yiban_id": "YOOC_YIBAN_ID",
        "user_id": "YOOC_USER_ID",
        "group_id": "YOOC_GROUP_ID",
        "exam_id": "YOOC_EXAM_ID",
        "examuser_id": "YOOC_EXAMUSER_ID",
    }
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            s[k] = v
    return s


def require(s, *keys):
    miss = [k for k in keys if not s.get(k)]
    if miss:
        raise SystemExit(
            f"缺少会话参数: {', '.join(miss)}\n"
            f"请填写 {SESSION_FILE} 或设置环境变量 YOOC_TOKEN / YOOC_YIBAN_ID / YOOC_USER_ID"
        )


def mask(v, keep=6):
    if not v:
        return "(未设置)"
    v = str(v)
    return v[:keep] + "…" + v[-4:] if len(v) > keep + 4 else v[:2] + "…"


def save(s):
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "init":
        if os.path.exists(SESSION_FILE):
            print(f"已存在: {SESSION_FILE}（不覆盖）")
            return 0
        save({"token": "", "yiban_id": "", "user_id": "",
              "group_id": "", "exam_id": "", "examuser_id": ""})
        print(f"已生成模板: {SESSION_FILE}")
        print("请填入 token / yiban_id / user_id / group_id / exam_id")
        print("提示: 推荐先用 yiban_sso.py 自动换取凭据，不必手填 token。")
        return 0
    s = load()
    print("当前生效会话:")
    for k in DEFAULTS:
        v = s.get(k)
        print(f"  {k:12s} = {mask(v) if k == 'token' else (v or '(未设置)')}")
    print(f"\n配置文件: {SESSION_FILE} {'(存在)' if os.path.exists(SESSION_FILE) else '(不存在)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
