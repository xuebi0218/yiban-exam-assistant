#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_keeper.py — 浏览器保活管理器（修复「多次开关 + 反复人机验证」）

════════════════════════════════════════════════════════════════
为什么之前的体验很差
════════════════════════════════════════════════════════════════

旧实现里，**每个操作各自 launch 一个全新浏览器再 close**：

    拉取试卷 -> launch A -> close A     （过一次人机验证）
    填写     -> launch B -> close B     （再过一次人机验证）
    再填写   -> launch C -> close C     （再过一次）

由于每次都是全新 context，会话不共享，人机验证要重复过，
而且窗口开了又关、关了又开，观感极差。
更糟的是流程结束时把浏览器关掉，导致用户**没法手工交卷**。

════════════════════════════════════════════════════════════════
本模块的做法
════════════════════════════════════════════════════════════════

  · 全局只维护**一个**持久浏览器上下文（单例）
  · 第一次操作时打开，之后所有操作复用
  · **流程结束后保持打开**，交给你复核和交卷
  · 只有显式调 close() 才真正关闭
  · 起一条专属工作线程（Playwright 有线程亲和性）

同时提供 close_all_on_exit=False 语义：进程退出不主动杀浏览器。
"""
import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROFILE = os.path.join(HERE, ".pwprofile_live")


class BrowserKeeper:
    """单例式保活浏览器。"""

    def __init__(self, profile=None, headless=False):
        self.profile = profile or PROFILE
        self.headless = headless
        self._pw = None
        self.ctx = None
        self.page = None
        self._q = None
        self._t = None
        self._ready = None
        self._lock = threading.Lock()
        self.opened_at = None

    # ------------------------------------------------------------ 线程
    def _worker(self):
        from playwright.sync_api import sync_playwright
        os.makedirs(self.profile, exist_ok=True)
        try:
            self._pw = sync_playwright().start()
            self.ctx = self._pw.chromium.launch_persistent_context(
                self.profile, headless=self.headless, no_viewport=True,
                args=["--start-maximized",
                      "--disable-blink-features=AutomationControlled"])
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        except Exception:
            self._ready.set()
            return
        self._ready.set()

        while True:
            fn = self._q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:
                pass

        # 只有收到显式退出信号才关
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _call(self, fn, timeout=300):
        if self._q is None:
            raise RuntimeError("浏览器未打开（请先调用 ensure()）")
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
            raise TimeoutError("浏览器操作超时")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    # ------------------------------------------------------------ 生命周期
    def ensure(self):
        """确保浏览器已打开；已开则直接复用。返回 (page 可用性)。"""
        with self._lock:
            if self._t is not None and self._t.is_alive() and self.page is not None:
                return True
            self._q = queue.Queue()
            self._ready = threading.Event()
            self._t = threading.Thread(target=self._worker, daemon=True)
            self._t.start()
            if not self._ready.wait(120):
                raise RuntimeError("浏览器启动超时")
            if self.page is None:
                raise RuntimeError("浏览器启动失败")
            self.opened_at = time.time()
            return True

    def alive(self):
        return bool(self.page is not None and self._t is not None and self._t.is_alive())

    def close(self):
        """显式关闭（用户要求关掉时才调）。"""
        with self._lock:
            try:
                if self._q:
                    self._q.put(None)
                if self._t:
                    self._t.join(timeout=30)
            except Exception:
                pass
            self.ctx = self.page = self._pw = None
            self._t = None

    # ------------------------------------------------------------ 常用操作
    def goto(self, url, timeout=60000, settle=1.5):
        def f():
            self.page.goto(url, timeout=timeout)
            time.sleep(settle)
            return self.page.url
        return self._call(f)

    def url(self):
        return self._call(lambda: self.page.url)

    def evaluate(self, js, arg=None):
        if arg is None:
            return self._call(lambda: self.page.evaluate(js))
        return self._call(lambda: self.page.evaluate(js, arg))

    def add_cookies(self, cookies):
        """在**专属线程内**注入 cookie。

        ⚠️ 千万不要在外面直接 `k.ctx.add_cookies(...)`：
        Playwright 的 sync 对象有 greenlet 线程亲和性，
        跨线程访问会抛 `greenlet.error: Cannot switch to a different thread`，
        并把进程的事件循环搞坏（表现为整个服务崩掉，实测踩过）。
        """
        def f():
            self.ctx.add_cookies(cookies)
            return True
        return self._call(f)

    def cookies(self):
        """在专属线程内读取全部 cookie。"""
        def f():
            return {c["name"]: c["value"] for c in self.ctx.cookies()}
        return self._call(f)

    def screenshot(self, path):
        def f():
            self.page.screenshot(path=path, full_page=False)
            return path
        return self._call(f)

    def info(self):
        """当前浏览器状态，供 UI 展示。"""
        if not self.alive():
            return {"alive": False}
        try:
            return {"alive": True, "url": self.url(),
                    "uptime": int(time.time() - (self.opened_at or time.time()))}
        except Exception:
            return {"alive": False}


# ---------------------------------------------------------------- 全局单例
_KEEPER = None


def get_keeper(headless=False):
    global _KEEPER
    if _KEEPER is None:
        _KEEPER = BrowserKeeper(headless=headless)
    return _KEEPER


def keeper_info():
    if _KEEPER is None:
        return {"alive": False}
    return _KEEPER.info()


def close_keeper():
    global _KEEPER
    if _KEEPER is not None:
        _KEEPER.close()
        _KEEPER = None
