#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_to_yooc.py — 扫码登录 yiban.cn → SSO 换优课凭证 → 直达考试页

════════════════════════════════════════════════════════════════
完整链路（全部实测确认过的环节）
════════════════════════════════════════════════════════════════

  1. yiban.cn 扫码登录
       GET /user/loginscan/qrcode          -> {qrcode, uuid}
       GET /user/loginscan/check?uuid=     -> waiting/scanned/confirmed/expired
       confirmed 时服务端在 .yiban.cn 域 Set-Cookie: yiban_user_token

  2. 关键：yiban_user_token 就是 SSO 的 access_token
       GET https://www.yooc.me/yiban_account/sso?access_token=<yiban_user_token>
         （带上 yiban_user_token 作 Cookie，且 allow_redirects=False）
       -> 302 + Set-Cookie: user_token / yiban_id / user_id

  3. 用这三项写 session.json，然后打开考试页

════════════════════════════════════════════════════════════════
用法
════════════════════════════════════════════════════════════════

  python src/scan_to_yooc.py            # 出码 → 等你扫 → 自动换凭证 → 开考试页
  python src/scan_to_yooc.py --no-open  # 只登录，不打开考试页
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yiban_sso as SSO          # noqa
from qr_login import QRSession, QR_TTL   # noqa

ZH = {"waiting": "等待扫码", "scanned": "已扫码，请在手机上确认",
      "confirmed": "已确认", "expired": "已过期（自动换码）"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true", help="登录后不打开考试页")
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    ok = False
    info = {}
    yiban_ck = {}

    print("\n" + "=" * 64)
    print("  步骤 1/3  易班 App 扫码登录")
    print("  （二维码 60 秒过期，会自动刷新）")
    print("=" * 64 + "\n", flush=True)

    qs = QRSession(headless=a.headless).open()
    try:
        # ---- 扫码，直到 confirmed ----
        qs.goto_login()
        deadline = time.time() + a.wait
        qr_uuid = None
        qr_born = 0
        last = None

        while time.time() < deadline:
            if not qr_uuid or time.time() - qr_born > QR_TTL:
                qr_url, qr_uuid = qs.new_qr()
                qr_born = time.time()
                print(f"[二维码] {qr_url}", flush=True)
                last = None

            st = qs.status(qr_uuid)
            if st != last:
                print(f"  状态: {ZH.get(st, st)}", flush=True)
                last = st

            if st == "confirmed":
                ok = True
                break
            if st == "expired":
                qr_uuid = None
                continue
            time.sleep(1.6)

        if not ok:
            print("\n[FAIL] 扫码未完成（超时）")
            return 2

        # ---- 读取 yiban 域 cookie（关键：yiban_user_token）----
        yiban_ck = qs.cookies_all()
        token = yiban_ck.get("yiban_user_token")
        print(f"\n[..] 已取得 yiban cookie: {list(yiban_ck)}")
        if not token:
            print("[FAIL] 扫码成功但没拿到 yiban_user_token")
            print(f"       现有 cookie: {list(yiban_ck)}")
            return 3
        print(f"[OK] yiban_user_token = {token}")
    finally:
        qs.close()

    # ---- 步骤 2：SSO 换优课凭证 ----
    print("\n" + "=" * 64)
    print("  步骤 2/3  用 yiban_user_token 走官方 SSO 换优课凭证")
    print("=" * 64 + "\n", flush=True)
    cookie_str = "; ".join(f"{k}={v}" for k, v in yiban_ck.items())
    ok2, ck2, detail = SSO.sso_exchange(token, yiban_cookie=cookie_str)
    if not ok2:
        print(f"[FAIL] SSO 交换失败: {detail}")
        return 4
    print(f"[OK] 取得: {list(ck2)}")

    vok, vd = SSO.verify(ck2["user_token"], ck2["yiban_id"], ck2["user_id"])
    s = SSO.save_session(ck2)
    if vok:
        exams = vd.get("data") or []
        print(f"[OK] 业务校验通过，可见考试 {len(exams)} 场")
        for e in exams[:5]:
            left = (e.get("repeatNumber") or 0) - (e.get("totalRepeats") or 0)
            stt = {3: "进行中", 4: "已完成", 5: "待开始"}.get(e.get("status"), e.get("status"))
            print(f"     examId={e.get('examId')} status={stt} score={e.get('score')} "
                  f"剩余重做={left} {str(e.get('name'))[:26]}")
    else:
        print(f"[WARN] 凭证已存但校验未过: {vd}")

    print(f"\n[OK] 已写入 session.json  yiban_id={s['yiban_id']}  user_id={s['user_id']}")

    # ---- 步骤 3：打开考试页 ----
    if not a.no_open:
        print("\n" + "=" * 64)
        print("  步骤 3/3  打开考试页")
        print("=" * 64 + "\n", flush=True)
        url = f"https://exam.yooc.me/group/{s['group_id']}/exams"
        print(f"[..] {url}")
        import webbrowser
        webbrowser.open(url)
        print("     已用系统浏览器打开。若登录态未同步，请在该浏览器里手动登录一次，")
        print("     或在操作台里点「① 拉取试卷」由 Playwright 接管。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
