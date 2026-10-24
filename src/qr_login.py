#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qr_login.py — 易班 App 扫码登录（会话一致版，修复「扫了没反应」）

════════════════════════════════════════════════════════════════
为什么之前"扫了没反应" —— 三个真实 bug
════════════════════════════════════════════════════════════════

BUG-1  会话割裂（最致命）
    申请二维码用无状态 urllib（临时会话 A），
    扫码确认后再新起一个 Playwright 浏览器（会话 C）去取 token。
    但扫码确认后的登录态只会下发给会话 A —— A 早已销毁。
    所以 **即使扫码成功，也永远换不到 token**。

BUG-2  二维码 60 秒就过期（实测 61s）
    前端 2 秒轮一次，看着像"没反应"，其实很快就 expired 了。

BUG-3  确认后重建二维码
    mode_login() 内部又申请了一次二维码，等于把用户刚扫的那张作废。

════════════════════════════════════════════════════════════════
正确架构：**整条链路只用一个浏览器上下文**
════════════════════════════════════════════════════════════════

    同一个 context：
      open yiban.cn/login
        -> 在页面内 fetch 申请二维码         (会话 A)
        -> 定时在页面内 fetch check           (同一个会话 A)
        -> status=confirmed 时,
           服务端把登录态 Set-Cookie 给会话 A
        -> 直接在同一 context 打开 yooc.me
        -> yooc 凭 A 的易班会话免密登录
        -> 读取 ctx.cookies() 拿到 user_token/yiban_id/user_id

因为全程同一个 context，cookie 自然贯通，不需要任何人工搬运。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
SESSION_FILE = os.path.join(HERE, "session.json")
NEED = ("user_token", "yiban_id", "user_id")
QR_TTL = 55          # 实测 61s 过期，留 6s 安全余量


def _verify(token, yiban_id, user_id, group_id=""):
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"token": token, "yibanId": yiban_id,
                                "userId": user_id, "groupId": group_id})
    req = urllib.request.Request(
        f"https://exambackend.yooc.me/api/exam/user/check?{q}",
        headers={"Origin": "https://exam.yooc.me",
                 "Referer": "https://exam.yooc.me/", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return bool(d.get("result")), d.get("data") or d
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def save_session(ck):
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
    json.dump(s, open(SESSION_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return s


class QRSession:
    """一个持久浏览器上下文里完成：出码 → 轮询 → 登优课 → 取 token。

    ⚠️ 线程亲和性：Playwright 同步对象**必须**始终在同一线程内使用
    （实测跨 run_in_executor 线程会抛 "Cannot switch to a different thread"）。
    因此本类内部起一条专属工作线程，所有 Playwright 操作都投递到该线程执行。

    on_qr(url, uuid)   : 拿到新二维码时回调（UI 用来渲染图片）
    on_status(status)  : 状态变化时回调
    """

    def __init__(self, profile=None, headless=False):
        self.profile = profile or os.path.join(HERE, ".pwprofile_scan")
        self.headless = headless
        self._pw = None
        self.ctx = None
        self.page = None
        self._t = None
        self._q = None
        self._ready = None

    # ------------------------------------------------ 专属线程
    def _worker(self):
        from playwright.sync_api import sync_playwright
        os.makedirs(self.profile, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile, headless=self.headless, no_viewport=True,
            args=["--start-maximized",
                  "--disable-blink-features=AutomationControlled"])
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._ready.set()
        while True:
            fn = self._q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:
                pass
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def _call(self, fn, timeout=180):
        """把操作投递到专属线程并等待结果。"""
        import threading
        box = {}
        done = threading.Event()

        def wrapped():
            try:
                box["v"] = fn()
            except Exception as e:
                box["e"] = e
            finally:
                done.set()

        self._q.put(wrapped)
        if not done.wait(timeout):
            raise TimeoutError("扫码操作超时")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def open(self):
        import queue as _q
        import threading
        self._q = _q.Queue()
        self._ready = threading.Event()
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._t.start()
        if not self._ready.wait(90):
            raise RuntimeError("浏览器启动超时")
        return self

    def close(self):
        try:
            if self._q:
                self._q.put(None)
            if self._t:
                self._t.join(timeout=25)
        except Exception:
            pass
        self.ctx = self.page = self._pw = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------ 步骤（全部走专属线程）
    def goto_login(self):
        def f():
            self.page.goto("https://www.yiban.cn/login", timeout=60000)
            time.sleep(1.2)
        return self._call(f)

    def new_qr(self):
        def f():
            r = self.page.evaluate("""async () => {
              try {
                const r = await fetch('/user/loginscan/qrcode', {credentials:'include'});
                const j = await r.json();
                if (j.code !== 200 || !j.data) return {err: j.message || 'bad'};
                return {qr: j.data.qrcode, uuid: j.data.uuid};
              } catch(e) { return {err: String(e)}; }
            }""")
            if not r or r.get("err"):
                raise RuntimeError(f"申请二维码失败: {r}")
            return r["qr"], r["uuid"]
        return self._call(f)

    def status(self, uuid):
        def f():
            return self.page.evaluate("""async (uuid) => {
              try {
                const r = await fetch('/user/loginscan/check?uuid=' + encodeURIComponent(uuid),
                                      {credentials:'include'});
                const j = await r.json();
                return (j.data && j.data.status) || 'unknown';
              } catch(e) { return 'error'; }
            }""", uuid)
        return self._call(f)

    def cookies(self):
        def f():
            out = {}
            for c in self.ctx.cookies():
                if c.get("name") in NEED:
                    out[c["name"]] = c["value"]
            return out
        return self._call(f)

    def cookies_all(self):
        """读出上下文里**全部** cookie（扫码后判断 yiban_user_token 是否下发）。"""
        def f():
            return {c["name"]: c["value"] for c in self.ctx.cookies()}
        return self._call(f)

    def yiban_token(self):
        """扫码登录后 yiban.cn 域下发的 yiban_user_token（即 SSO 的 access_token）。"""
        def f():
            for c in self.ctx.cookies():
                if c.get("name") == "yiban_user_token" and c.get("value"):
                    return c["value"]
            return None
        return self._call(f)

    def goto_yooc(self):
        """扫码确认后，同一 context 直接上优课，凭易班会话免密登录。"""
        def f():
            self.page.goto("https://www.yooc.me/", timeout=60000)
            time.sleep(2.5)
            ck = {}
            for c in self.ctx.cookies():
                if c.get("name") in NEED:
                    ck[c["name"]] = c["value"]
            if all(k in ck for k in NEED):
                return ck
            # 兜底：经易班顶部「易班优课」入口跳转（官方指引的路径）
            try:
                self.page.goto("https://www.yiban.cn/", timeout=60000)
                time.sleep(1.5)
                self.page.click("#YOOC_MENU", timeout=6000)
                time.sleep(3.5)
            except Exception:
                try:
                    self.page.goto("https://www.yooc.me/", timeout=60000)
                    time.sleep(2.5)
                except Exception:
                    pass
            ck = {}
            for c in self.ctx.cookies():
                if c.get("name") in NEED:
                    ck[c["name"]] = c["value"]
            return ck
        return self._call(f, timeout=120)

    # ------------------------------------------------ 主流程
    def run(self, on_qr=None, on_status=None, wait_s=300):
        """自动续码 + 轮询 + 换 token。返回 (ok, info)。"""
        self.goto_login()

        deadline = time.time() + wait_s
        qr_url = qr_uuid = None
        qr_born = 0
        last = None

        while time.time() < deadline:
            # 二维码到期就自动换一张（这是"没反应"的第二个根因）
            if not qr_uuid or time.time() - qr_born > QR_TTL:
                qr_url, qr_uuid = self.new_qr()
                qr_born = time.time()
                if on_qr:
                    on_qr(qr_url, qr_uuid)
                last = None

            st = self.status(qr_uuid)
            if st != last:
                if on_status:
                    on_status(st)
                last = st

            if st == "confirmed":
                ck = self.goto_yooc()
                miss = [k for k in NEED if k not in ck]
                if miss:
                    return False, {"error": f"扫码已确认，但优课未建立会话（缺 {miss}）",
                                   "stage": "yooc"}
                ok, info = _verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
                if not ok:
                    return False, {"error": f"token 校验失败: {info}", "stage": "verify"}
                s = save_session(ck)
                return True, {"yiban_id": s["yiban_id"], "user_id": s["user_id"],
                              "token": s["token"]}
            if st == "expired":
                qr_uuid = None       # 下一轮自动换码
                continue
            time.sleep(1.6)

        return False, {"error": "等待扫码超时"}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    ZH = {"waiting": "等待扫码", "scanned": "已扫码，请在手机上确认",
          "confirmed": "已确认", "expired": "已过期（自动换码）"}
    with QRSession(headless=a.headless) as qs:
        print("\n" + "=" * 62)
        print("  请用手机易班 App 扫描下面二维码")
        print("  （二维码 60 秒过期，会自动刷新）")
        print("=" * 62 + "\n", flush=True)

        def on_qr(url, uuid):
            print(f"[二维码] {url}", flush=True)

        def on_status(st):
            print(f"  状态: {ZH.get(st, st)}", flush=True)

        ok, info = qs.run(on_qr=on_qr, on_status=on_status, wait_s=a.wait)

    if ok:
        print(f"\n[OK] 扫码登录成功")
        print(f"     yiban_id = {info['yiban_id']}")
        print(f"     user_id  = {info['user_id']}")
        print(f"     token    = {info['token'][:8]}…")
        return 0
    print(f"\n[FAIL] {info.get('error')}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
