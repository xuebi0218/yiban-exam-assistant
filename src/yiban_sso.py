#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yiban_sso.py — 用易班 yiban_user_token 换取优课登录态（真正的官方通道）

════════════════════════════════════════════════════════════════
机制（由用户提供真实 URL 确认，非猜测）
════════════════════════════════════════════════════════════════

  yiban.cn 页面点「易班优课」会跳转到：

    https://www.yooc.me/yiban_account/sso?access_token=<yiban_user_token>

  其中 access_token 就是 yiban.cn 域下的 cookie `yiban_user_token`。

  SSO 成功后，服务端在 .yooc.me 域 Set-Cookie，下发三项：
    user_token / yiban_id / user_id

  这三项即后续所有考试 API 所需的凭证。

  ⚠️ 注意：yiban_user_token ≠ user_token，必须经 SSO 交换，不能直接混用。
     （实测：把 yiban_user_token 直接当 token 传给业务接口会返回
       「用户token失效」403；只有 /user/check 这类宽松接口会误判为通过。）

════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════

  python src/yiban_sso.py "yiban_user_token=xxx; YB_SSID=yyy; ..."
      # 粘贴 yiban.cn 的整段 cookie，自动提取 yiban_user_token 并完成 SSO

  python src/yiban_sso.py --token YOUR_YIBAN_USER_TOKEN
      # 已知 access_token，只做交换

  python src/yiban_sso.py --check
      # 校验当前 session.json 是否仍有效
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
SESSION_FILE = os.path.join(HERE, "session.json")
SSO_URL = "https://www.yooc.me/yiban_account/sso"
NEED = ("user_token", "yiban_id", "user_id")


# ---------------------------------------------------------------- 工具
def extract_yiban_token(blob):
    """从 yiban.cn 的 cookie 文本里提取 yiban_user_token。"""
    if not blob:
        return None
    m = re.search(r"yiban_user_token=([0-9a-zA-Z]+)", blob)
    if m:
        return m.group(1)
    # 也允许直接传 token 本身（32位十六进制）
    m = re.fullmatch(r"\s*([0-9a-f]{32})\s*", blob)
    return m.group(1) if m else None


def parse_set_cookie(headers):
    """从一堆 Set-Cookie 头里抽出三项。"""
    out = {}
    for h in headers:
        # 一条头里可能含多个 cookie
        for part in re.split(r",(?=[^;]+?=)", h or ""):
            m = re.match(r"\s*([A-Za-z_][\w\-]*)=([^;]*)", part)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if k in NEED and v:
                out[k] = v
    return out


def http_get(url, cookie=None, timeout=30, follow=True):
    """发 GET，返回 (status, headers_map, body, setcookie_list)。

    用 requests 而非 urllib：urllib 的 opener 会忽略手工设置的 Cookie 头，
    导致 SSO 拿不到 yiban 会话（实测踩过，200 但只回匿名 sessionid）。
    """
    try:
        import requests
    except ImportError:
        requests = None

    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.yiban.cn/",
    }
    if cookie:
        h["Cookie"] = cookie

    if requests is not None:
        # ⚠️ 必须 allow_redirects=False：SSO 的 302 响应头里才有三项 Set-Cookie；
        #    一旦自动跟随，requests 会把它们吸收进 jar 而响应头就看不到了。
        r = requests.get(url, headers=h, timeout=timeout, allow_redirects=False)
        sc = []
        try:
            sc = r.raw.headers.getlist("Set-Cookie") or []
        except Exception:
            sc = []
        if not sc:
            v = r.headers.get("Set-Cookie")
            sc = [v] if v else []
        return r.status_code, dict(r.headers), r.content, sc

    # 退路：urllib（显式注入 Cookie 头）
    req = urllib.request.Request(url, headers=h)
    opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            sc = r.headers.get_all("Set-Cookie") or []
            return r.status, dict(r.headers), r.read(), sc
    except urllib.error.HTTPError as e:
        sc = e.headers.get_all("Set-Cookie") or [] if e.headers else []
        return e.code, dict(e.headers or {}), e.read() or b"", sc


# ---------------------------------------------------------------- SSO
def sso_exchange(access_token, yiban_cookie=None, verbose=True):
    """用 access_token 换优课三项 cookie。

    实测结论（修正早期判断）：
      · **只需要 access_token**，不需要额外 cookie。
        早先「必须带 yiban_user_token cookie」的结论是错的——
        真正的原因是当时 HTTP 库自动跟随了 302，导致拿不到 Set-Cookie。
        改成 allow_redirects=False 后，什么都不带也能成功。

    返回 (ok, data, detail)
    """
    url = f"{SSO_URL}?access_token={urllib.parse.quote(access_token)}"
    if verbose:
        print(f"[..] SSO 请求: {url}")

    # 传了 cookie 就带上（无害），没传也能成功
    extra = yiban_cookie or f"yiban_user_token={access_token}"

    jar = {}
    cur = url
    for hop in range(6):
        own = "; ".join(f"{k}={v}" for k, v in jar.items())
        ck = "; ".join(x for x in (extra, own) if x)
        try:
            status, hdrs, body, sc = http_get(cur, cookie=ck, timeout=30)
        except Exception as e:
            return False, {}, {"error": f"请求失败 {type(e).__name__}: {e}", "url": cur}

        if verbose:
            print(f"       hop{hop+1}: HTTP {status}  len={len(body)}")

        for h in sc:
            for part in re.split(r",(?=[^;]+?=)", h):
                m = re.match(r"\s*([A-Za-z_][\w\-]*)=([^;]*)", part)
                if m:
                    jar[m.group(1)] = m.group(2).strip()

        got = {k: jar[k] for k in NEED if k in jar}
        if len(got) == len(NEED):
            return True, got, {"hops": hop + 1, "url": cur}

        loc = hdrs.get("Location") or hdrs.get("location")
        if status in (301, 302, 303, 307, 308) and loc:
            cur = urllib.parse.urljoin(cur, loc)
            continue
        break

    partial = {k: jar.get(k) for k in NEED}
    return False, partial, {
        "error": "SSO 未下发完整的 user_token/yiban_id/user_id",
        "got": {k: v for k, v in partial.items() if v},
        "all_cookies": list(jar),
        "url": cur,
        "hint": "access_token 可能已过期（去 www.yiban.cn 重新取 yiban_user_token）",
    }


def verify(token, yiban_id, user_id, group_id=""):
    """用**业务接口**校验（不是宽松的 user/check）。"""
    q = urllib.parse.urlencode({"groupId": group_id, "token": token,
                                "yibanId": yiban_id, "userId": user_id})
    url = f"https://exambackend.yooc.me/api/exam/list/get?{q}"
    try:
        status, hdrs, body, _ = http_get(url, timeout=25)
        d = json.loads(body.decode("utf-8", "replace"))
        return bool(d.get("result")), d
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def save_session(ck, group_id=None, exam_id=None):
    s = {}
    if os.path.exists(SESSION_FILE):
        try:
            s = json.load(open(SESSION_FILE, encoding="utf-8"))
        except Exception:
            s = {}
    s["token"] = ck["user_token"]
    s["yiban_id"] = ck["yiban_id"]
    s["user_id"] = ck["user_id"]
    s.setdefault("group_id", group_id or "")
    s.setdefault("exam_id", exam_id or "")
    s.setdefault("examuser_id", "")
    json.dump(s, open(SESSION_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return s


def check_current():
    if not os.path.exists(SESSION_FILE):
        print("[FAIL] 没有 session.json")
        return 2
    s = json.load(open(SESSION_FILE, encoding="utf-8"))
    # session.json 里叫 token，内部别名是 user_token
    miss = [k for k in ("token", "yiban_id", "user_id") if not s.get(k)]
    if miss:
        print(f"[FAIL] session.json 缺 {miss}")
        return 2
    ok, d = verify(s["token"], s["yiban_id"], s["user_id"],
                   s.get("group_id") or "")
    if ok:
        exams = d.get("data") or []
        print(f"[OK] token 有效  yiban_id={s['yiban_id']}  user_id={s['user_id']}")
        for e in exams[:5]:
            left = (e.get("repeatNumber") or 0) - (e.get("totalRepeats") or 0)
            st = {3: "进行中", 4: "已完成", 5: "待开始"}.get(e.get("status"), e.get("status"))
            print(f"     examId={e.get('examId')} status={st} score={e.get('score')} "
                  f"剩余重做={left} {str(e.get('name'))[:24]}")
        return 0
    print(f"[FAIL] token 无效: {d}")
    return 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cookie", nargs="?", default="",
                    help="yiban.cn 的 cookie 串，或 32 位 access_token")
    ap.add_argument("--token", default="", help="直接给 yiban_user_token")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--group", default=None)
    ap.add_argument("--exam", default=None)
    a = ap.parse_args()

    if a.check:
        return check_current()

    src = a.token or a.cookie
    if not src and not sys.stdin.isatty():
        src = sys.stdin.read()
    if not src:
        print("用法:")
        print('  python src/yiban_sso.py "yiban_user_token=xxx; ..."')
        print("  python src/yiban_sso.py --token <access_token>")
        print("  python src/yiban_sso.py --check")
        return 1

    access_token = extract_yiban_token(src)
    if not access_token:
        print("[FAIL] 未能从输入里找到 yiban_user_token")
        print("       请粘贴 yiban.cn 的完整 cookie，或直接给 32 位 token")
        return 2

    print(f"[..] access_token = {access_token}")
    # 若输入是完整 cookie 串，整段带上；否则至少带 token 本身
    yban_ck = src if (";" in src or "yiban_user_token=" in src) else None
    ok, ck, detail = sso_exchange(access_token, yiban_cookie=yban_ck)
    if not ok:
        print(f"\n[FAIL] SSO 交换失败")
        for k, v in detail.items():
            print(f"       {k} = {v}")
        return 3

    print(f"[OK] 已取得优课凭证: {list(ck)}")
    ok2, d = verify(ck["user_token"], ck["yiban_id"], ck["user_id"],
                    a.group or "")
    if not ok2:
        print(f"[WARN] 凭证已取得但业务接口校验未通过: {d}")
        print("       已照样写入 session.json，可稍后重试")
    else:
        exams = d.get("data") or []
        print(f"[OK] 业务校验通过，可见考试 {len(exams)} 场")
        for e in exams[:5]:
            print(f"     examId={e.get('examId')} status={e.get('status')} "
                  f"score={e.get('score')} {str(e.get('name'))[:26]}")

    s = save_session(ck, a.group, a.exam)
    print(f"\n[OK] 已写入 session.json")
    print(f"     yiban_id = {s['yiban_id']}")
    print(f"     user_id  = {s['user_id']}")
    print(f"     token    = {s['token'][:8]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
