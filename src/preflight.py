#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preflight.py — 新账号接入前的完整自检

不做任何写操作，只验证:
  1. 会话配置完整（token / yiban_id / user_id）
  2. 接口连通 + 账号身份正确
  3. 课群权限、考试列表
  4. 题库就绪
  5. 上次作答结果（若有）
  6. 教材语料就绪

用法:
  python src/preflight.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0"
BASE = "https://exambackend.yooc.me/api/exam"


def get(path, params, timeout=25):
    q = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    u = f"{BASE}/{path}?{q}"
    req = urllib.request.Request(u, headers={
        "Origin": "https://exam.yooc.me", "Referer": "https://exam.yooc.me/",
        "User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ok(msg):
    print(f"  [OK]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")


def warn(msg):
    print(f"  [WARN] {msg}")


def main():
    print("=" * 62)
    print("开考前自检")
    print("=" * 62)

    # 1. 会话
    print("\n1) 会话配置")
    from session import load, SESSION_FILE
    s = load()
    missing = [k for k in ("token", "yiban_id", "user_id") if not s.get(k)]
    if missing:
        bad(f"缺少: {', '.join(missing)}  -> 请填写 {SESSION_FILE}")
        return 2
    ok(f"token={s['token'][:6]}…  yiban_id={s['yiban_id']}  user_id={s['user_id']}")
    if not s.get("group_id"):
        warn("未设置 group_id，考试列表将无法查询")
    if not s.get("exam_id"):
        warn("未设置 exam_id")

    secret = {"token": s["token"], "yibanId": s["yiban_id"], "userId": s["user_id"]}

    # 2. 连通与身份
    print("\n2) 接口连通与身份")
    try:
        r = get("user/check", {"groupId": s.get("group_id", ""), **secret})
        if r.get("result"):
            role = (r.get("data") or {}).get("role")
            ok(f"身份校验通过, role={role}")
        else:
            bad(f"校验失败: {r}")
    except Exception as e:
        bad(f"请求失败: {type(e).__name__}: {e}")
        print("     -> 若为 403/超时: 检查 cookie 是否过期, 或网络策略")
        return 3

    # 3. 考试列表
    print("\n3) 考试列表")
    if s.get("group_id"):
        try:
            r = get("list/get", {"groupId": s["group_id"], **secret})
            exams = r.get("data") or []
            if not exams:
                warn("该课群下没有考试")
            for e in exams:
                st = {3: "进行中", 4: "已完成", 5: "待开始"}.get(e.get("status"), e.get("status"))
                left = (e.get("repeatNumber") or 0) - (e.get("totalRepeats") or 0)
                ok(f"examId={e['examId']} 「{e['name'][:24]}」 status={st} "
                   f"score={e.get('score')} 剩余重做={left}")
        except Exception as e:
            bad(f"列表查询失败: {e}")

    # 4. 题库
    print("\n4) 题库")
    try:
        from qbank import load_bank
        b = load_bank()
        n = len(b["entries"])
        if n == 0:
            warn("题库为空 —— 新账号首次作答将全部依赖教材/大模型")
        else:
            ok(f"{n} 条真答案（来自已回捞的作答）")
            from collections import Counter
            c = Counter(v["variants"][0]["type"] for v in b["entries"].values())
            print(f"         题型: {dict(c)}")
    except Exception as e:
        bad(f"题库读取失败: {e}")

    # 5. 教材语料
    print("\n5) 教材语料")
    import glob as _glob
    found = False
    for p in sorted(_glob.glob(os.path.join(HERE, "..", "data", "corpus_*.txt"))):
        size = os.path.getsize(p)
        ok(f"{os.path.basename(p)}: {size:,} bytes")
        found = True
    if not found:
        warn("未找到教材语料。题库未命中时将无法兜底求解。")
        print("     请把教材转成纯文本放到 data/corpus_*.txt")

    print("\n" + "=" * 62)
    print("自检完成。开考前请确认: 剩余重做次数 > 0, 且考试窗口未过期。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
