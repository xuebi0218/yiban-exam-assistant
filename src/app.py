#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 可视化操作台（aiohttp，零额外依赖）

界面能力:
  · 账号/场次配置（token / yiban_id / group_id / exam_id）
  · 一键流程：自检 → 开考 → 拉题 → 生成答案 → 填充 → 门禁 → 回捞
  · 每题的来源徽章（题库/教材/待复核）+ 置信度 + 证据
  · REVIEW 题集中展示，支持在线填写后回灌
  · 实时日志流（SSE）

启动:
  python src/app.py            # 打开 http://127.0.0.1:8765
"""
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from aiohttp import web  # noqa

import session as SESS  # noqa
import login as LOGIN  # noqa
from qbank import load_bank  # noqa
import orchestrator as ORCH  # noqa
import auto_run as AR  # noqa

STATIC = os.path.join(HERE, "webui")
RUNS = {}          # run_id -> 状态对象
QR_SESSIONS = {}   # uuid -> 二维码会话
QR_SESSION = None  # 当前扫码浏览器会话（同一上下文贯穿）
QR_STATE = {}      # 扫码状态
LOGQ = queue.Queue()

# 答题页状态探测（放在模块级，供多个 handler 复用）
def cookie_list(s):
    """会话 -> Playwright cookie 结构。

    只在 BrowserKeeper 的专属线程内通过 add_cookies() 注入，
    切勿直接操作 ctx（greenlet 线程亲和性）。
    """
    out = []
    if s.get("token"):
        out.append({"name": "user_token", "value": s["token"],
                    "domain": ".yooc.me", "path": "/"})
    if s.get("yiban_id"):
        out.append({"name": "yiban_id", "value": s["yiban_id"],
                    "domain": ".yooc.me", "path": "/"})
    if s.get("user_id"):
        out.append({"name": "user_id", "value": s["user_id"],
                    "domain": ".yooc.me", "path": "/"})
    return out


STATE_JS_PAGE = """() => {
  const t = document.body.innerText;
  const m = t.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
  const cap = t.match(/点击图中[^\\n]{0,30}/);
  return {idx: m?parseInt(m[1],10):null, total: m?parseInt(m[2],10):null,
          captcha: cap?cap[0]:null,
          hasInput: document.querySelectorAll('input.exam-input').length,
          hasLi: document.querySelectorAll('li').length};
}"""


# ---------------------------------------------------------------- 日志
def log(msg, level="info"):
    rec = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": str(msg)}
    LOGQ.put(rec)
    print(f"[{rec['t']}] {msg}", flush=True)


# ---------------------------------------------------------------- API
async def api_login_qr_start(request):
    """启动扫码会话（同一个浏览器上下文贯穿始终，修复会话割裂）。

    返回当前二维码；前端拿到后用 /api/login/qr/state 轮询。
    """
    try:
        import qr_login as QR
        global QR_SESSION
        # 关掉旧的
        if QR_SESSION is not None:
            try:
                await asyncio.get_event_loop().run_in_executor(None, QR_SESSION.close)
            except Exception:
                pass
            QR_SESSION = None

        def _start():
            qs = QR.QRSession(headless=True).open()
            qs.goto_login()
            qr, uuid = qs.new_qr()
            return qs, qr, uuid, time.time()

        qs, qr, uuid, born = await asyncio.get_event_loop().run_in_executor(None, _start)
        QR_SESSION = qs
        QR_STATE.update({"qr": qr, "uuid": uuid, "born": born,
                         "status": "waiting", "yiban_id": None, "user_id": None,
                         "error": None, "done": False})
        log(f"扫码会话已启动 uuid={uuid[:12]}…")
        return web.json_response({"ok": True, "qr": qr, "uuid": uuid, "ttl": QR.QR_TTL})
    except Exception as e:
        log(f"启动扫码失败: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_qr_state(request):
    """轮询扫码状态。**确认后自动走 SSO 换优课凭证**（真正的免密链路）。"""
    import qr_login as QR
    import yiban_sso as SSO
    st = QR_STATE
    if QR_SESSION is None or not st.get("uuid"):
        return web.json_response({"ok": False, "error": "扫码会话未启动"}, status=400)

    # 二维码到期 -> 同一会话内换新码
    if not st.get("done") and time.time() - st.get("born", 0) > QR.QR_TTL:
        try:
            def _renew():
                return QR_SESSION.new_qr()

            qr, uuid = await asyncio.get_event_loop().run_in_executor(None, _renew)
            st.update({"qr": qr, "uuid": uuid, "born": time.time(), "status": "waiting"})
            log(f"二维码已刷新 uuid={uuid[:12]}…")
            return web.json_response({"ok": True, "status": "waiting",
                                      "qr": qr, "uuid": uuid, "renewed": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"刷新二维码失败: {e}"}, status=400)

    if st.get("done"):
        return web.json_response({"ok": True, "status": st.get("status"),
                                  "yiban_id": st.get("yiban_id"),
                                  "user_id": st.get("user_id"),
                                  "exams": st.get("exams") or [],
                                  "error": st.get("error")})

    try:
        status = await asyncio.get_event_loop().run_in_executor(
            None, lambda: QR_SESSION.status(st["uuid"]))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

    st["status"] = status

    if status == "confirmed":
        log("扫码已确认，开始换取优课凭证…")

        def _exchange():
            # 1) 取 yiban_user_token（= SSO 的 access_token）
            token = QR_SESSION.yiban_token()
            if not token:
                return None, None, {"error": "扫码成功但未下发 yiban_user_token"}
            # 2) 走官方 SSO
            ok, ck, detail = SSO.sso_exchange(token, verbose=False)
            if not ok:
                return None, token, detail
            vok, vd = SSO.verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
            s = SSO.save_session(ck)
            return s, token, {"verified": vok, "exams": (vd.get("data") or []) if vok else [],
                              "detail": vd}

        try:
            saved, token, extra = await asyncio.get_event_loop().run_in_executor(
                None, _exchange)
            if saved:
                exams = [{"examId": e.get("examId"), "status": e.get("status"),
                          "score": e.get("score"), "name": e.get("name")}
                         for e in (extra.get("exams") or [])[:5]]
                st.update({"done": True, "status": "confirmed",
                           "yiban_id": saved["yiban_id"], "user_id": saved["user_id"],
                           "exams": exams})
                log(f"扫码登录成功 yiban_id={saved['yiban_id']} 可见考试 {len(exams)} 场", "ok")
            else:
                st.update({"done": True, "status": "confirmed",
                           "error": (extra or {}).get("error") if isinstance(extra, dict) else str(extra)})
                log(f"扫码后续失败: {st.get('error')}", "error")
        except Exception as e:
            st.update({"done": True, "status": "confirmed", "error": str(e)})
            log(f"扫码后续异常: {e}", "error")

    return web.json_response({"ok": True, "status": st.get("status"),
                              "yiban_id": st.get("yiban_id"),
                              "user_id": st.get("user_id"),
                              "exams": st.get("exams") or [],
                              "error": st.get("error")})


async def api_start_exam(request):
    """登录后：直接打开考试页（含重做/开始考试的可选自动点击）。"""
    try:
        s = SESS.load()
        SESS.require(s, "token", "yiban_id", "user_id")
        import yiban_sso as SSO
        ok, d = await asyncio.get_event_loop().run_in_executor(
            None, lambda: SSO.verify(s["token"], s["yiban_id"], s["user_id"],
                                     s.get("group_id") or ""))
        if not ok:
            return web.json_response({"ok": False, "error": f"登录无效: {d}"}, status=400)
        exams = d.get("data") or []
        gid = (s.get("group_id") or "").strip()
        if not gid or "" in gid:
            return web.json_response({"ok": False, "error": "请先在 session.json 填写 group_id"}, status=400)
        url = f"https://exam.yooc.me/group/{gid}/exams"

        def _open():
            import webbrowser
            webbrowser.open(url)

        await asyncio.get_event_loop().run_in_executor(None, _open)
        log(f"已打开考试页 {url}", "ok")
        return web.json_response({"ok": True, "url": url,
                                  "exams": [{"examId": e.get("examId"), "status": e.get("status"),
                                             "score": e.get("score"),
                                             "left": (e.get("repeatNumber") or 0) - (e.get("totalRepeats") or 0),
                                             "name": e.get("name")} for e in exams]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_qr_cancel(request):
    global QR_SESSION
    if QR_SESSION is not None:
        try:
            await asyncio.get_event_loop().run_in_executor(None, QR_SESSION.close)
        except Exception:
            pass
        QR_SESSION = None
    QR_STATE.update({"qr": None, "uuid": None, "status": None, "done": False})
    return web.json_response({"ok": True})


async def api_login_qr(request):
    """申请一枚易班扫码二维码（前端直接渲染给用户扫）。"""
    try:
        import login_scan as LS
        qr, uuid = await asyncio.get_event_loop().run_in_executor(None, LS.fetch_qr)
        QR_SESSIONS[uuid] = {"qr": qr, "created": time.time()}
        log(f"已生成扫码二维码 uuid={uuid[:12]}…")
        return web.json_response({"ok": True, "qr": qr, "uuid": uuid})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_qr_poll(request):
    """轮询扫码状态；确认后建立优课会话并抓 token。"""
    try:
        import login_scan as LS
        uuid = request.query.get("uuid") or ""
        if not uuid:
            return web.json_response({"ok": False, "error": "缺少 uuid"}, status=400)
        status = await asyncio.get_event_loop().run_in_executor(
            None, lambda: LS.check_scan(uuid))
        if status != "confirmed":
            return web.json_response({"ok": True, "status": status or "waiting"})
        # 已确认 -> 复用浏览器完整链路抓 token
        rc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: LS.mode_login(wait_s=120))
        s = SESS.load()
        if rc == 0:
            log("扫码登录成功，已写入会话", "ok")
            return web.json_response({"ok": True, "status": "confirmed",
                                      "yiban_id": s.get("yiban_id"),
                                      "user_id": s.get("user_id")})
        return web.json_response({"ok": False, "status": "confirmed",
                                  "error": "扫码成功但未能取得优课 token，请用浏览器登录"},
                                 status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_scan_browser(request):
    """完整扫码链路（打开窗口，人工扫码）。"""
    try:
        import login_scan as LS
        rc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: LS.mode_login(wait_s=300))
        s = SESS.load()
        if rc == 0:
            log("扫码登录成功", "ok")
            return web.json_response({"ok": True, "yiban_id": s.get("yiban_id")})
        return web.json_response({"ok": False, "error": "扫码登录未完成"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_sso(request):
    """易班 SSO 登录：粘 yiban.cn 的 cookie，自动换优课凭证。

    这是真正的官方通道：access_token 就是 yiban_user_token。
    """
    try:
        body = await request.json()
        blob = (body.get("cookie") or "").strip()
        if not blob:
            return web.json_response({"ok": False, "error": "请粘贴 yiban.cn 的 cookie"}, status=400)

        import yiban_sso as SSO
        token = SSO.extract_yiban_token(blob)
        if not token:
            return web.json_response({"ok": False,
                                      "error": "未找到 yiban_user_token（请粘 yiban.cn 的整段 cookie）"},
                                     status=400)
        log(f"SSO 交换中 access_token={token[:8]}…")

        def _do():
            return SSO.sso_exchange(token, yiban_cookie=blob, verbose=False)

        ok, ck, detail = await asyncio.get_event_loop().run_in_executor(None, _do)
        if not ok:
            log(f"SSO 失败: {detail.get('error')}", "error")
            return web.json_response({"ok": False, "error": detail.get("error"),
                                      "detail": detail}, status=400)

        # 用业务接口校验（user/check 会误判）
        def _verify():
            return SSO.verify(ck["user_token"], ck["yiban_id"], ck["user_id"])

        vok, vd = await asyncio.get_event_loop().run_in_executor(None, _verify)
        s = SSO.save_session(ck)
        exams = (vd.get("data") or []) if vok else []
        if vok:
            log(f"SSO 登录成功 yiban_id={s['yiban_id']} user_id={s['user_id']}", "ok")
            for e in exams[:3]:
                log(f"  考试 {e.get('examId')} status={e.get('status')} score={e.get('score')}")
        else:
            log(f"凭证已存但业务校验未过: {vd}", "warn")

        return web.json_response({"ok": True, "yiban_id": s["yiban_id"],
                                  "user_id": s["user_id"],
                                  "verified": vok,
                                  "exams": [{"examId": e.get("examId"), "status": e.get("status"),
                                             "score": e.get("score"), "name": e.get("name")}
                                            for e in exams[:5]],
                                  "warn": None if vok else str(vd)})
    except Exception as e:
        log(f"SSO 异常: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_paste(request):
    """粘贴 cookie 接入账号。"""
    try:
        body = await request.json()
        blob = body.get("cookie") or ""
        if not blob.strip():
            return web.json_response({"ok": False, "error": "cookie 为空"}, status=400)
        ck = LOGIN.parse_cookie_blob(blob)
        miss = [k for k in LOGIN.NEED if k not in ck]
        if miss:
            log(f"cookie 解析缺少: {miss}", "warn")
            return web.json_response({"ok": False, "error": f"未解析到: {', '.join(miss)}",
                                      "parsed": ck}, status=400)
        ok, info = LOGIN.verify(ck["user_token"], ck["yiban_id"], ck["user_id"])
        if not ok:
            log(f"cookie 校验失败: {info}", "error")
            return web.json_response({"ok": False, "error": f"cookie 无效: {info}"}, status=400)
        s = LOGIN.apply_to_session(ck)
        log(f"登录成功（粘贴模式） yiban_id={s['yiban_id']} user_id={s['user_id']}", "ok")
        return web.json_response({"ok": True, "yiban_id": s["yiban_id"],
                                  "user_id": s["user_id"], "role": info.get("role")})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_browser(request):
    """打开可见浏览器，人工登录后自动捕获 cookie。"""
    try:
        def _go():
            return LOGIN.login_browser(wait_s=600)

        rc = await asyncio.get_event_loop().run_in_executor(None, _go)
        s = SESS.load()
        if rc == 0:
            log("登录成功（浏览器模式）", "ok")
            return web.json_response({"ok": True, "yiban_id": s.get("yiban_id"),
                                      "user_id": s.get("user_id")})
        return web.json_response({"ok": False, "error": "未捕获到有效登录态（超时或未完成登录）"},
                                 status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_check(request):
    """校验当前登录是否有效。

    ⚠️ 必须用**业务接口**判断：/user/check 对任意 token 都会返回 result:true，
    会把过期的/冒充的 token 误判为有效（实测踩过）。
    """
    try:
        s = SESS.load()
        miss = [k for k in ("token", "yiban_id", "user_id") if not s.get(k)]
        if miss:
            return web.json_response({"ok": False, "error": f"缺少 {miss}"}, status=400)
        import yiban_sso as SSO
        ok, d = await asyncio.get_event_loop().run_in_executor(
            None, lambda: SSO.verify(s["token"], s["yiban_id"], s["user_id"],
                                     s.get("group_id") or ""))
        if ok:
            exams = d.get("data") or []
            log(f"登录有效 yiban_id={s['yiban_id']} 可见考试 {len(exams)} 场", "ok")
            return web.json_response({"ok": True, "yiban_id": s["yiban_id"],
                                      "user_id": s["user_id"], "exams": len(exams)})
        log(f"登录失效: {d}", "warn")
        return web.json_response({"ok": False, "error": f"token 无效: {d}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_login_reuse(request):
    """复用本机已登录的浏览器 profile。"""
    try:
        def _go():
            return LOGIN.reuse_profile()

        rc = await asyncio.get_event_loop().run_in_executor(None, _go)
        if rc == 0:
            s = SESS.load()
            log("复用 profile 成功", "ok")
            return web.json_response({"ok": True, "yiban_id": s.get("yiban_id")})
        return web.json_response({"ok": False, "error": "profile 无有效登录态，请用浏览器登录"},
                                 status=400)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_session(request):
    s = SESS.load()
    return web.json_response({
        "ok": True,
        "session": {k: (SESS.mask(v) if k == "token" else v) for k, v in s.items()},
        "raw_hint": "token 出于安全不显示明文",
    })


async def api_session_save(request):
    body = await request.json()
    s = SESS.load()
    for k in ("token", "yiban_id", "user_id", "group_id", "exam_id"):
        v = (body.get(k) or "").strip()
        if v:
            s[k] = v
    SESS.save(s)
    log("会话配置已保存", "ok")
    return web.json_response({"ok": True})


async def api_state(request):
    """查询远端考试状态。"""
    try:
        s = SESS.load()
        SESS.require(s, "token", "yiban_id", "user_id")
        st = AR.get_state(s)
        return web.json_response({"ok": True, "state": st})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_browser_open(request):
    """手动打开保活浏览器，并**直接导航到考试页**（不停在 about:blank）。"""
    import browser_keeper as BK
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        target = (body.get("url") or "").strip()

        def _open():
            k = BK.get_keeper(headless=False)
            k.ensure()
            s = SESS.load()
            # 必须走 k.add_cookies()（专属线程内），不能直接碰 k.ctx
            try:
                k.add_cookies(cookie_list(s))
            except Exception:
                pass
            # 决定落地页
            if target:
                url = target
            else:
                gid = (s.get("group_id") or "").strip()
                if gid:
                    url = f"https://exam.yooc.me/group/{gid}/exams"
                else:
                    # 未配置课群 ID 时落到优课首页，而不是拼出无效链接
                    url = "https://www.yooc.me/"
            cur = k.url()
            if (cur or "").startswith("about:") or not cur:
                k.goto(url, settle=2.0)
            return k.url()

        url = await asyncio.get_event_loop().run_in_executor(None, _open)
        log(f"浏览器已打开（保活）→ {url}", "ok")
        return web.json_response({"ok": True, "url": url})
    except Exception as e:
        log(f"打开浏览器失败: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_browser_peek(request):
    """只读查看保活浏览器当前页面内容（诊断用，不点击任何东西）。"""
    import browser_keeper as BK
    k = BK.get_keeper()
    if not k.alive():
        return web.json_response({"ok": False, "error": "浏览器未打开"}, status=400)
    try:
        r = k.evaluate("""() => {
          const t = document.body ? document.body.innerText : '';
          return {title: document.title, url: location.href,
                  textLen: t.length, head: t.slice(0, 500),
                  hasLogin: /登录|密码|账号/.test(t),
                  hasExam: /考试|交卷|开始/.test(t)};
        }""")
        return web.json_response({"ok": True, **r})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_browser_state(request):
    """当前保活浏览器状态。"""
    import browser_keeper as BK
    return web.json_response({"ok": True, **BK.keeper_info()})


async def api_browser_close(request):
    """显式关闭浏览器（人工交卷完成后再点）。"""
    import browser_keeper as BK
    try:
        await asyncio.get_event_loop().run_in_executor(None, BK.close_keeper)
        log("浏览器已关闭", "ok")
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_browser_focus(request):
    """把浏览器窗口提到前台，方便人工交卷。"""
    import browser_keeper as BK
    k = BK.get_keeper()
    if not k.alive():
        return web.json_response({"ok": False, "error": "浏览器未打开"}, status=400)
    try:
        def f():
            k.page.bring_to_front()
            return k.url()
        url = k._call(f)
        log(f"已前置浏览器窗口 {url}", "ok")
        return web.json_response({"ok": True, "url": url})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_bank(request):
    b = load_bank()
    e = b.get("entries") or {}
    return web.json_response({
        "ok": True,
        "entries": len(e),
        "harvested": (b.get("meta") or {}).get("harvested", 0),
        "conflicts": len((b.get("meta") or {}).get("conflicts") or []),
    })


async def api_paper(request):
    """拉取当前试卷（复用保活浏览器，不再新开一个）。"""
    try:
        s = SESS.load()
        st = AR.get_state(s)
        if st.get("status") != 3 or not st.get("examuserId"):
            return web.json_response({"ok": False,
                                      "error": f"考试未进行中 (status={st.get('status')})"}, status=400)
        s2 = dict(s)
        s2["examuser_id"] = str(st["examuserId"])
        p = os.path.join(HERE, "paper_ui.json")

        def _pull():
            import browser_keeper as BK
            k = BK.get_keeper(headless=False)
            k.ensure()
            log("浏览器已就绪（保活，不会自动关闭）")
            # 注入登录 cookie（走专属线程，不能直接碰 k.ctx）
            try:
                k.add_cookies(cookie_list(s2))
            except Exception:
                pass
            url = "https://exam.yooc.me/group/%s/exam/%s/take" % (s2["group_id"], s2["exam_id"])
            cur = k.url()
            if "exam.yooc.me" not in (cur or ""):
                k.goto(url, settle=1.5)
            info = k.evaluate(STATE_JS_PAGE)
            if not info.get("idx"):
                # 等验证码
                log("等待答题页就绪（请在浏览器里过人机验证）…", "warn")
                t0 = time.time()
                while time.time() - t0 < 900:
                    info = k.evaluate(STATE_JS_PAGE)
                    if info.get("idx"):
                        break
                    time.sleep(1.5)
            if not info.get("idx"):
                return None
            log(f"答题页就绪 第 {info['idx']}/{info['total']} 题", "ok")
            paper = k.evaluate(AR.PAPER_JS, {
                "examuserId": s2["examuser_id"], "token": s2["token"],
                "yibanId": s2["yiban_id"]})
            raw = [{"subjectId": x["id"], "type": x["type"], "title": [x["title"]],
                    "option": [[o] for o in x["opts"]], "answer": x.get("answerField")}
                   for x in paper]
            json.dump(raw, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            return raw

        raw = await asyncio.get_event_loop().run_in_executor(None, _pull)
        if not raw:
            return web.json_response({"ok": False, "error": "未拉到试卷（人机验证未通过？）"}, status=400)
        log(f"已拉取 {len(raw)} 题", "ok")
        return web.json_response({"ok": True, "n": len(raw), "paper_file": p})
    except Exception as e:
        log(f"拉题失败: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_solve(request):
    """对已拉取的试卷生成答案计划。"""
    try:
        p = os.path.join(HERE, "paper_ui.json")
        if not os.path.exists(p):
            return web.json_response({"ok": False, "error": "请先拉题"}, status=400)
        paper = json.load(open(p, encoding="utf-8"))
        o = ORCH.Orchestrator()
        log("开始生成答案（题库优先 → 教材兜底）")
        res = await asyncio.get_event_loop().run_in_executor(None, o.solve_paper, paper)
        json.dump(res, open(os.path.join(HERE, "plan_ui.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        st = res["stat"]
        log(f"答案就绪：题库 {st['qbank']} / 教材 {st['textbook_auto']} / "
            f"待复核 {st['textbook_soft']} / 人工 {st['review']}", "ok")
        return web.json_response({"ok": True, "stat": st, "plan": res["plan"]})
    except Exception as e:
        log(f"生成答案失败: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_plan(request):
    p = os.path.join(HERE, "plan_ui.json")
    if not os.path.exists(p):
        return web.json_response({"ok": False, "error": "尚无答案计划"}, status=404)
    d = json.load(open(p, encoding="utf-8"))
    return web.json_response({"ok": True, "stat": d.get("stat"), "plan": d.get("plan")})


async def api_review_save(request):
    """人工补填 REVIEW 题。"""
    body = await request.json()
    items = body.get("items") or {}
    p = os.path.join(HERE, "plan_ui.json")
    if not os.path.exists(p):
        return web.json_response({"ok": False, "error": "尚无答案计划"}, status=400)
    d = json.load(open(p, encoding="utf-8"))
    n = 0
    for r in d["plan"]:
        sid = str(r.get("sid"))
        if sid in items and str(items[sid]).strip():
            r["answer"] = str(items[sid]).strip()
            r["tier"] = "manual"
            r["conf"] = 1.0
            r["source"] = "人工"
            r["why"] = "人工补填"
            n += 1
    # 重新统计
    o = ORCH.Orchestrator()
    o._tally(d["plan"])
    d["stat"] = o.stats
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"人工补填 {n} 题", "ok")
    return web.json_response({"ok": True, "filled": n, "stat": d["stat"]})


async def api_apply(request):
    """把答案计划填进网页并跑门禁。"""
    try:
        plan_path = os.path.join(HERE, "plan_ui.json")
        paper_path = os.path.join(HERE, "paper_ui.json")
        if not (os.path.exists(plan_path) and os.path.exists(paper_path)):
            return web.json_response({"ok": False, "error": "请先拉题并生成答案"}, status=400)
        s = SESS.load()
        st = AR.get_state(s)
        if st.get("status") != 3:
            return web.json_response({"ok": False, "error": "考试未进行中"}, status=400)

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        include_soft = bool(body.get("include_soft", True))

        def _fill():
            import browser_keeper as BK
            o = ORCH.Orchestrator()
            d = json.load(open(plan_path, encoding="utf-8"))
            ans = o.to_fill_payload(d, include_soft=include_soft)
            paper = json.load(open(paper_path, encoding="utf-8"))
            papers = [{"id": p["subjectId"], "type": p["type"],
                       "opts": [o2[0] if isinstance(o2, list) else o2 for o2 in (p.get("option") or [])],
                       "title": (p.get("title") or [""])[0]} for p in paper]

            k = BK.get_keeper(headless=False)
            k.ensure()
            log("复用同一浏览器填写（不会自动关闭，留给人工交卷）")
            page_url = k.url()
            if "exam.yooc.me" not in (page_url or ""):
                log("浏览器不在答题页，正在导航…", "warn")
                k.goto("https://exam.yooc.me/group/%s/exam/%s/take"
                       % (s["group_id"], s["exam_id"]), settle=1.5)
                t0 = time.time()
                while time.time() - t0 < 900:
                    info = k.evaluate(STATE_JS_PAGE)
                    if info.get("idx"):
                        break
                    time.sleep(1.5)

            log("开始填写")
            res = k.evaluate(AR.FILL_JS, {"paper": papers, "answers": ans})
            log(f"填写完成: {res}")
            rb = k.evaluate(AR.READBACK_JS)
            json.dump(rb, open(os.path.join(HERE, "readback_ui.json"), "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)

            r1 = r2 = r3 = 0
            empty = []
            for i, p in enumerate(papers, 1):
                v = rb.get(str(i))
                if not v:
                    r3 += 1; empty.append(i); continue
                if p["type"] == "completion":
                    if not (v.get("value") or "").strip():
                        r3 += 1; empty.append(i)
                else:
                    if not v.get("picked"):
                        r3 += 1; empty.append(i)
            return {"ok": True, "fill": res, "readback": rb,
                    "gate": {"sel": r1, "text": r2, "empty": r3},
                    "empty_idx": empty,
                    "browser_left_open": True}

        out = await asyncio.get_event_loop().run_in_executor(None, _fill)
        if not out.get("ok"):
            return web.json_response(out, status=400)
        g = out["gate"]
        log(f"门禁：空值 {g['empty']} 处", "ok" if g["empty"] == 0 else "warn")
        return web.json_response(out)
    except Exception as e:
        log(f"填写失败: {e}", "error")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_harvest(request):
    try:
        s = SESS.load()
        st = AR.get_state(s)
        if st.get("status") != 4:
            return web.json_response({"ok": False, "error": "考试未交卷"}, status=400)
        eid = str(st["examuserId"])

        def _h():
            import subprocess
            r = subprocess.run([sys.executable, os.path.join(HERE, "qbank.py"), "harvest", eid],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            return r.stdout + (r.stderr or "")

        out = await asyncio.get_event_loop().run_in_executor(None, _h)
        for line in (out or "").splitlines():
            if line.strip():
                log(line.strip())
        return web.json_response({"ok": True, "output": out, "score": st.get("score")})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_preflight(request):
    try:
        s = SESS.load()
        SESS.require(s, "token", "yiban_id", "user_id")
        st = AR.get_state(s)
        b = load_bank()
        return web.json_response({"ok": True, "state": st,
                                  "bank": len(b.get("entries") or {})})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def api_logs(request):
    """SSE 实时日志。客户端断开时要安静退出，不能抛异常。"""
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)
    try:
        while True:
            try:
                rec = LOGQ.get_nowait()
                await resp.write(f"data: {json.dumps(rec, ensure_ascii=False)}\n\n".encode())
            except queue.Empty:
                await resp.write(b": ping\n\n")
                await asyncio.sleep(0.5)
    except (ConnectionResetError, asyncio.CancelledError):
        pass          # 页面关闭/刷新，正常现象
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------- 静态
async def index(request):
    p = os.path.join(STATIC, "index.html")
    if not os.path.exists(p):
        return web.Response(text="UI 未找到: " + p, status=404)
    return web.FileResponse(p)


async def static_file(request):
    name = request.match_info["name"]
    p = os.path.join(STATIC, name)
    if os.path.exists(p) and os.path.isfile(p):
        return web.FileResponse(p)
    return web.Response(status=404)


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/static/{name}", static_file),
        web.get("/api/session", api_session),
        web.post("/api/session", api_session_save),
        web.post("/api/login/paste", api_login_paste),
        web.post("/api/login/browser", api_login_browser),
        web.get("/api/login/check", api_login_check),
        web.post("/api/login/reuse", api_login_reuse),
        web.post("/api/login/qr", api_login_qr),
        web.post("/api/login/sso", api_login_sso),
        web.post("/api/login/qr/start", api_login_qr_start),
        web.get("/api/login/qr/state", api_login_qr_state),
        web.post("/api/start-exam", api_start_exam),
        web.get("/api/browser", api_browser_state),
        web.get("/api/browser/peek", api_browser_peek),
        web.post("/api/browser/open", api_browser_open),
        web.post("/api/browser/close", api_browser_close),
        web.post("/api/browser/focus", api_browser_focus),
        web.post("/api/login/qr/cancel", api_login_qr_cancel),
        web.get("/api/login/qr/poll", api_login_qr_poll),
        web.post("/api/login/scan", api_login_scan_browser),
        web.get("/api/state", api_state),
        web.get("/api/bank", api_bank),
        web.get("/api/preflight", api_preflight),
        web.post("/api/paper", api_paper),
        web.post("/api/solve", api_solve),
        web.get("/api/plan", api_plan),
        web.post("/api/review", api_review_save),
        web.post("/api/apply", api_apply),
        web.post("/api/harvest", api_harvest),
        web.get("/api/logs", api_logs),
    ])
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  可视化操作台: http://127.0.0.1:{port}\n", flush=True)
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)
