#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
login.py — 账号接入：把「拿 token」这件事产品化

背景（实测结论）:
  · yooc.me 登录页 **没有扫码入口**，只有「账号 + 密码 + 验证码」
    （验证码由 /getCapthaKey 下发，type=0 数美 / type=1 网易易盾，均为人机验证）
  · 登录成功后才下发 user_token / yiban_id / user_id 三个 cookie
  · 这三个 cookie **不是 HttpOnly**，可用 JS 读取

因此提供三种接入方式，按省心程度排序:

  A. cookie 粘贴   —— 最快。在已登录的浏览器 F12 里复制一段，粘进来即可
  B. 浏览器登录    —— 打开可见浏览器，你手输账号密码+过验证码，
                      脚本自动捕获三项 cookie 并回写 session.json
  C. 复用现有会话  —— 若本机浏览器已登录过，直接从持久化 profile 里读

用法:
  python src/login.py paste "<cookie字符串>"
  python src/login.py browser     # 打开浏览器，人工登录后自动捕获
  python src/login.py reuse       # 从 .pwprofile 复用
  python src/login.py check       # 校验当前 token 是否有效
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SESSION_FILE = os.path.join(HERE, "session.json")
PROFILE = os.path.join(HERE, ".pwprofile")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0"
NEED = ("user_token", "yiban_id", "user_id")


# ---------------------------------------------------------------- cookie 解析
def parse_cookie_blob(blob):
    """从任意形态的 cookie 文本里提取三项。

    支持:
      · 浏览器 F12 里 document.cookie 的原始串  a=1; b=2
      · 从 Application/Cookies 面板复制的表格行
      · 一整段 header 文本
    """
    out = {}
    if not blob:
        return out
    # 统一分隔：优先按分号；也兼容换行/制表
    for part in re.split(r"[;\n\t]+", blob):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        if k in NEED and v:
            out[k] = v
    # 兜底：整段直接正则找
    for k in NEED:
        if k not in out:
            m = re.search(rf"{k}[=:\s]+([\w\-\.]+)", blob)
            if m:
                out[k] = m.group(1)
    return out


def apply_to_session(ck, keep_group=True):
    s = {}
    if os.path.exists(SESSION_FILE):
        try:
            s = json.load(open(SESSION_FILE, encoding="utf-8"))
        except Exception:
            s = {}
    s["token"] = ck["user_token"]
    s["yiban_id"] = ck["yiban_id"]
    s["user_id"] = ck["user_id"]
    s.setdefault("group_id", "")
    s.setdefault("exam_id", "")
    s.setdefault("examuser_id", "")
    json.dump(s, open(SESSION_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return s


# ---------------------------------------------------------------- 校验
def verify(token, yiban_id, user_id, group_id=""):
    """校验凭证。

    ⚠️ 用**业务接口** list/get，不用 user/check。
    实测 user/check 对任意 token 都返回 result:true，会把过期/冒充的 token
    误判为有效（踩过这个坑）。
    """
    q = urllib.parse.urlencode({
        "token": token, "yibanId": yiban_id, "userId": user_id,
        "groupId": group_id})
    u = f"https://exambackend.yooc.me/api/exam/list/get?{q}"
    req = urllib.request.Request(u, headers={
        "Origin": "https://exam.yooc.me", "Referer": "https://exam.yooc.me/",
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("result"):
            exams = d.get("data") or []
            return True, {"exams": len(exams)}
        return False, d
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 浏览器捕获
def read_cookies_from_ctx(ctx):
    out = {}
    for c in ctx.cookies():
        n = c.get("name")
        if n in NEED:
            out[n] = c.get("value")
    return out


def login_browser(wait_s=600):
    """打开可见浏览器，由人工完成登录 + 验证码，脚本轮询捕获 cookie。"""
    from playwright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    print("\n" + "=" * 62)
    print("  浏览器即将打开。请在这个窗口里：")
    print("    1) 输入易班账号 + 密码")
    print("    2) 通过人机验证（数美 / 网易易盾）")
    print("    3) 登录成功后不用关窗口，脚本会自动读取 cookie")
    print("=" * 62 + "\n", flush=True)

    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        PROFILE, headless=False, no_viewport=True,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.yooc.me/login", timeout=60000)
        except Exception:
            pass

        t0 = time.time()
        last = None
        while time.time() - t0 < wait_s:
            ck = read_cookies_from_ctx(ctx)
            if all(k in ck for k in NEED):
                # 校验一次，防止抓到旧的
                ok, info = verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
                if ok:
                    s = apply_to_session(ck)
                    print(f"\n[OK] 已捕获并写入 session.json")
                    print(f"     yiban_id = {s['yiban_id']}")
                    print(f"     user_id  = {s['user_id']}")
                    print(f"     token    = {s['token'][:8]}…")
                    return 0
                last = info
            if int(time.time() - t0) % 10 < 2:
                got = [k for k in NEED if k in ck]
                print(f"  等待登录… {int(time.time()-t0)}s  已见 cookie: {got or '无'}", flush=True)
            time.sleep(1.5)
        print(f"\n[FAIL] 超时未捕获到有效登录态。最后校验结果: {last}")
        return 2
    finally:
        try:
            ctx.close(); pw.stop()
        except Exception:
            pass


def reuse_profile():
    """从持久化 profile 直接读（浏览器已登录过）。"""
    from playwright.sync_api import sync_playwright
    if not os.path.isdir(PROFILE):
        print(f"[FAIL] 没有可复用的 profile: {PROFILE}")
        return 2
    pw = sync_playwright().start()
    ctx = None
    try:
        ctx = pw.chromium.launch_persistent_context(PROFILE, headless=True)
        ck = read_cookies_from_ctx(ctx)
        miss = [k for k in NEED if k not in ck]
        if miss:
            print(f"[FAIL] profile 里缺少: {miss}  -> 请用 browser 模式登录")
            return 2
        ok, info = verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
        if not ok:
            print(f"[FAIL] cookie 已失效: {info}")
            return 2
        s = apply_to_session(ck)
        print(f"[OK] 复用成功 yiban_id={s['yiban_id']} user_id={s['user_id']}")
        return 0
    finally:
        try:
            if ctx: ctx.close()
            pw.stop()
        except Exception:
            pass


def check():
    s = {}
    if os.path.exists(SESSION_FILE):
        s = json.load(open(SESSION_FILE, encoding="utf-8"))
    miss = [k for k in ("token", "yiban_id", "user_id") if not s.get(k)]
    if miss:
        print(f"[FAIL] session.json 缺少: {miss}")
        return 2
    ok, info = verify(s["token"], s["yiban_id"], s["user_id"],
                      s.get("group_id") or "")
    if ok:
        print(f"[OK] token 有效  role={info.get('role')}  yiban_id={s['yiban_id']}")
        return 0
    print(f"[FAIL] token 无效或过期: {info}")
    return 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["paste", "browser", "reuse", "check"])
    ap.add_argument("payload", nargs="?", default="")
    a = ap.parse_args()

    if a.mode == "check":
        return check()

    if a.mode == "browser":
        return login_browser()

    if a.mode == "reuse":
        return reuse_profile()

    # paste
    blob = a.payload
    if not blob and not sys.stdin.isatty():
        blob = sys.stdin.read()
    if not blob:
        print("用法: python src/login.py paste \"user_token=...; yiban_id=...; user_id=...\"")
        return 1
    ck = parse_cookie_blob(blob)
    miss = [k for k in NEED if k not in ck]
    if miss:
        print(f"[FAIL] 未解析到: {miss}")
        print(f"       解析结果: {ck}")
        print("\n提示: 登录 https://www.yooc.me 后，在 F12 控制台执行 document.cookie 复制全部")
        return 2
    ok, info = verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
    if not ok:
        print(f"[FAIL] cookie 无效: {info}")
        return 3
    s = apply_to_session(ck)
    print(f"[OK] 已写入 session.json")
    print(f"     yiban_id = {s['yiban_id']}")
    print(f"     user_id  = {s['user_id']}")
    print(f"     token    = {s['token'][:8]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
