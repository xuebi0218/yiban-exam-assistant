#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_run.py — 端到端答题自动化 v3（Playwright 可见浏览器）

方案 C：Playwright 启动可见 Chromium，用户在该窗口过验证码，其余全自动。

关键设计:
  · headless=False —— 用户必须能看到并点击验证码
  · 持久化 profile(.pwprofile) —— 验证码通过后同会话复用
  · 轮询等待验证码通过（默认 15 分钟窗口），通过后自动继续
  · 填写 → 门禁 → 保持浏览器打开等用户交卷
  · 检测到 status=4（已交卷）自动回捞并退出

用法:
  python src/auto_run.py auto      # 一键：开考(如需)→等验证码→拉题→填写→门禁→等交卷→回捞
  python src/auto_run.py fill      # 只做拉题+填写+门禁（考试需在进行中）
  python src/auto_run.py state     # 查看状态
  python src/auto_run.py harvest   # 回捞
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0"
BASE = "https://exambackend.yooc.me/api/exam"
LETTERS = "ABCDE"
PROFILE = os.path.join(HERE, ".pwprofile")
CAPTCHA_WAIT_S = 900     # 验证码最大等待 15 分钟
SUBMIT_WAIT_S = 3600     # 交卷最大等待 60 分钟


def ok(m): print(f"  [OK]   {m}", flush=True)
def bad(m): print(f"  [FAIL] {m}", flush=True)
def info(m): print(f"  [..]   {m}", flush=True)
def warn(m): print(f"  [WARN] {m}", flush=True)
def step(m): print(f"\n{'='*62}\n{m}\n{'='*62}", flush=True)


def norm(s):
    return re.sub(r"[\s\u3000，、,|]+", "", unicodedata.normalize("NFKC", s or "")).strip()


def norm_text(s):
    s = re.sub(r"^[A-Z][.、]\s*", "", s or "")
    return re.sub(r"[\s\u3000]+", "", s).strip()


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def load_session():
    from session import load, require
    s = load()
    require(s, "token", "yiban_id", "user_id")
    return s


def api_get(path, params, timeout=25):
    q = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    req = urllib.request.Request(f"{BASE}/{path}?{q}", headers={
        "Origin": "https://exam.yooc.me", "Referer": "https://exam.yooc.me/",
        "User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_state(s):
    secret = {"token": s["token"], "yibanId": s["yiban_id"], "userId": s["user_id"]}
    data = api_get("detail/get", {**secret, "examId": s["exam_id"]}).get("data") or {}
    first = (api_get("list/get", {**secret, "groupId": s["group_id"]}).get("data") or [{}])[0]
    return {
        "examuserId": data.get("examuserId") or 0,
        "status": data.get("status"),
        "score": first.get("score"),
        "left": (first.get("repeatNumber") or 0) - (first.get("totalRepeats") or 0),
        "expired": first.get("expired"),
    }


# ---------------------------------------------------------------- 浏览器
def launch(s, headless=False):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    os.makedirs(PROFILE, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        PROFILE, headless=headless, no_viewport=True,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    inject_cookies(ctx, s)
    return pw, ctx, page


def inject_cookies(ctx, s):
    ck = [
        {"name": "user_token", "value": s["token"], "domain": ".yooc.me", "path": "/"},
        {"name": "yiban_id", "value": s["yiban_id"], "domain": ".yooc.me", "path": "/"},
        {"name": "user_id", "value": s["user_id"], "domain": ".yooc.me", "path": "/"},
    ]
    try:
        ctx.add_cookies(ck)
    except Exception:
        pass


def inject_cookies_into(ctx, s):
    """幂等注入（保活浏览器复用同一 context，重复注入无害）。"""
    return inject_cookies(ctx, s)


STATE_JS = """() => {
  const t = document.body.innerText;
  const m = t.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
  const cap = t.match(/点击图中[^\\n]{0,30}/);
  return {idx: m?parseInt(m[1],10):null, total: m?parseInt(m[2],10):null,
          captcha: cap?cap[0]:null,
          hasInput: document.querySelectorAll('input.exam-input').length,
          hasLi: document.querySelectorAll('li').length};
}"""

PAPER_JS = """async (p) => {
  const r = await fetch(`https://exambackend.yooc.me/api/exam/paper/get?examuserId=${p.examuserId}&token=${p.token}&yibanId=${p.yibanId}`, {credentials:'include'});
  const j = await r.json();
  const subs = [];
  for (const sec of (j.data||[])) for (const s of (sec.subjects||[])) subs.push(s);
  return subs.map(s => ({id: s.subjectId, type: s.type,
    opts: (s.option||[]).map(o=>Array.isArray(o)?o[0]:o),
    title: (s.title||[''])[0], answerField: s.answer || null}));
}"""

FILL_JS = """(payload) => {
  const PP = payload.paper, ANS = payload.answers;
  const normText = s => (s||'').replace(/^[A-Z][.、]\\s*/,'').replace(/[\\s\\u3000]+/g,'').trim();
  const LETTERS = 'ABCDE';
  const plan = {};
  for (let i = 1; i <= PP.length; i++) {
    const p = PP[i-1], a = ANS[String(p.id)];
    if (!a) continue;
    if (p.type === 'completion') plan[i] = {kind:'text', value:a.a};
    else {
      const want = [];
      for (const ch of (a.a.toUpperCase().match(/[A-Z]/g) || [])) {
        const k = LETTERS.indexOf(ch);
        if (k >= 0 && k < p.opts.length) want.push(normText(p.opts[k]));
      }
      plan[i] = {kind:'sel', want};
    }
  }
  const curIdx = () => { const m = document.body.innerText.match(/(\\d+)\\s*\\/\\s*(\\d+)/); return m?[parseInt(m[1],10),parseInt(m[2],10)]:null; };
  const nav = t => new Promise(res => {
    const b = [...document.querySelectorAll('button')].find(x=>x.innerText.trim()===t);
    if (!b) return res(false);
    b.click(); setTimeout(()=>res(true), 185);
  });
  const setText = (el,v) => {
    const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');
    d.set.call(el,v); el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
  };
  const liList = () => [...document.querySelectorAll('li')].filter(li=>/^[A-Z][.、]/.test(li.innerText.trim()));
  return (async () => {
    let g = 0;
    while ((curIdx()||[1])[0] > 1 && g++ < 300) await nav('上一题');
    const log = [];
    g = 0;
    while (g++ < 300) {
      const cur = curIdx(); if (!cur) break;
      const i = cur[0], total = cur[1];
      const p = plan[i];
      if (!p) log.push(i+':skip');
      else if (p.kind === 'text') {
        const inp = document.querySelector('input.exam-input');
        if (inp) { setText(inp, p.value); log.push(i+':T'); } else log.push(i+':noinput');
      } else {
        const wantSet = new Set(p.want);
        for (let pass=0; pass<2; pass++)
          for (const li of liList()) {
            const should = wantSet.has(normText(li.innerText));
            if (li.className.includes('_c') !== should) { li.click(); await new Promise(r=>setTimeout(r,235)); }
          }
        log.push(i+':S');
      }
      if (i >= total) break;
      if (!await nav('下一题')) break;
      if ((curIdx()||[0])[0] === i) break;
    }
    return {filled: log.filter(x=>/:(T|S)$/.test(x)).length, skipped: log.filter(x=>x.endsWith('skip')).length};
  })();
}"""

READBACK_JS = """() => {
  const curIdx = () => { const m = document.body.innerText.match(/(\\d+)\\s*\\/\\s*(\\d+)/); return m?parseInt(m[1],10):null; };
  const nav = t => new Promise(res => {
    const b = [...document.querySelectorAll('button')].find(x=>x.innerText.trim()===t);
    if (!b) return res(false);
    b.click(); setTimeout(()=>res(true), 175);
  });
  const normText = s => (s||'').replace(/^[A-Z][.、]\\s*/,'').replace(/[\\s\\u3000]+/g,'').trim();
  return (async () => {
    let g = 0;
    while ((curIdx()||1) > 1 && g++ < 300) await nav('上一题');
    const RB = {};
    g = 0;
    while (g++ < 300) {
      const i = curIdx(); if (!i) break;
      const inp = document.querySelector('input.exam-input');
      if (inp) RB[i] = {kind:'text', value: inp.value || ''};
      else {
        const lis = [...document.querySelectorAll('li')].filter(li=>/^[A-Z][.、]/.test(li.innerText.trim()));
        RB[i] = {kind:'sel', picked: lis.filter(l=>l.className.includes('_c')).map(l=>normText(l.innerText))};
      }
      if (!await nav('下一题')) break;
      if (curIdx() === i) break;
    }
    return RB;
  })();
}"""


def wait_page_ready(page, timeout_s=CAPTCHA_WAIT_S):
    """轮询等待答题页就绪；期间提示用户在浏览器里过验证码。"""
    t0 = time.time()
    last_hint = None
    while time.time() - t0 < timeout_s:
        try:
            st = page.evaluate(STATE_JS)
            if st.get("idx"):
                return st, None
            hint = st.get("captcha")
            if hint and hint != last_hint:
                last_hint = hint
                print("\n" + "!" * 62, flush=True)
                print(f"  ⚠️  请在浏览器窗口中完成验证码: {hint}", flush=True)
                print(f"     已等待 {int(time.time()-t0)}s / 上限 {timeout_s}s", flush=True)
                print("!" * 62, flush=True)
            elif int(time.time() - t0) % 60 < 2:
                info(f"等待中... {int(time.time()-t0)}s (hint={hint})")
        except Exception:
            pass
        time.sleep(1.5)
    st = page.evaluate(STATE_JS)
    return st, st.get("captcha") or "timeout"


# ---------------------------------------------------------------- stages
def stage_auto(s):
    st = get_state(s)
    step("当前状态")
    print(json.dumps(st, ensure_ascii=False, indent=1))

    if st["status"] == 3 and st["examuserId"]:
        ok(f"已有进行中的作答 examuserId={st['examuserId']}，直接进入填写")
    elif st["status"] == 4:
        if st["left"] <= 0:
            bad("剩余重做次数为 0，无法开考")
            return 2
        return do_start_then_fill(s)
    else:
        bad(f"状态异常 status={st['status']}")
        return 2

    return do_fill(s)


def do_start_then_fill(s):
    """在 Playwright 里执行重做+开考+填写。"""
    step("启动浏览器（可见）")
    pw, ctx, page = launch(s, headless=False)
    try:
        info("打开考试列表")
        page.goto("https://exam.yooc.me/group/%s/exams" % s["group_id"], timeout=60000)
        time.sleep(2)

        btn = page.locator("button", has_text="重做试卷")
        if btn.count() == 0:
            bad("未找到「重做试卷」按钮")
            return 3
        btn.first.click()
        time.sleep(1.5)
        cf = page.locator("button", has_text="确定")
        if cf.count():
            cf.last.click()
            time.sleep(3)

        sb = page.locator("button", has_text="开始考试")
        if sb.count():
            sb.first.click()
            time.sleep(1.5)
            cf2 = page.locator("button", has_text="确定")
            if cf2.count():
                cf2.last.click()
                time.sleep(4)

        info(f"URL: {page.url}")
        return after_page_ready(s, pw, ctx, page)
    except Exception as e:
        bad(f"开考异常: {e}")
        try:
            ctx.close(); pw.stop()
        except Exception:
            pass
        return 3


def do_fill(s):
    step("启动浏览器（可见）")
    pw, ctx, page = launch(s, headless=False)
    try:
        page.goto("https://exam.yooc.me/group/%s/exam/%s/take" % (s["group_id"], s["exam_id"]),
                  timeout=60000)
        time.sleep(2)
        return after_page_ready(s, pw, ctx, page)
    except Exception as e:
        bad(f"导航异常: {e}")
        try:
            ctx.close(); pw.stop()
        except Exception:
            pass
        return 3


def after_page_ready(s, pw, ctx, page):
    st = get_state(s)
    if st["status"] != 3 or not st["examuserId"]:
        bad(f"考试未就绪 (status={st['status']})")
        return 2
    eid = str(st["examuserId"])
    info(f"examuserId = {eid}")

    print("\n等待答题页就绪（请在浏览器里过验证码）...", flush=True)
    info_page, err = wait_page_ready(page)
    if not info_page.get("idx"):
        bad(f"未就绪: {err}")
        info(f"URL: {page.url}")
        warn("浏览器保持打开，请手动过验证码后重跑 fill")
        return 4
    ok(f"答题页就绪: 第 {info_page['idx']}/{info_page['total']} 题")

    # 拉题
    info("拉取试卷...")
    paper = page.evaluate(PAPER_JS, {"examuserId": eid, "token": s["token"],
                                     "yibanId": s["yiban_id"]})
    if not paper:
        bad("拉题失败")
        return 3
    cts = {str(p.get("answerField")) for p in paper if p.get("answerField")}
    info(f"{len(paper)} 题, 密文种类 {len(cts)}")
    if len(cts) <= 3:
        warn("密文种类 <= 3 → 诱饵数据，不可用作答案")

    paper_path = os.path.join(HERE, "live_paper_auto.json")
    raw = [{"subjectId": p["id"], "type": p["type"], "title": [p["title"]],
            "option": [[o] for o in p["opts"]], "answer": p.get("answerField")} for p in paper]
    json.dump(raw, open(paper_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 生成答案
    plan_path = os.path.join(HERE, "plan_auto.json")
    info("生成答案...")
    r = subprocess.run([sys.executable, os.path.join(HERE, "exam_pipeline.py"),
                        "build", "--paper", paper_path, "--out", plan_path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if r.stdout:
        print(r.stdout.strip())
    if r.returncode != 0:
        bad("答案生成失败")
        if r.stderr:
            print(r.stderr.strip()[:500], file=sys.stderr)
        return 3

    plan = json.load(open(plan_path, encoding="utf-8"))["plan"]
    ans = {}
    for i, p in enumerate(paper, 1):
        pl = plan.get(str(i))
        if not pl or not pl.get("a"):
            continue
        ans[str(p["id"])] = ({"a": letters_of(pl["a"]), "t": "s"} if pl["k"] == "sel"
                             else {"a": pl["a"], "t": "t"})

    # 填写
    info(f"填写 {len(ans)} 题...")
    res = page.evaluate(FILL_JS, {"paper": paper, "answers": ans})
    info(f"填写结果: {res}")

    # 门禁
    info("三道门禁校验...")
    rb = page.evaluate(READBACK_JS)
    json.dump(rb, open(os.path.join(HERE, "readback_auto.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    r1 = r2 = r3 = 0
    for i in range(1, len(paper) + 1):
        p = paper[i - 1]
        v = rb.get(str(i)) or rb.get(i)
        if not v:
            r3 += 1
            continue
        a = ans.get(str(p["id"]))
        if p["type"] == "completion":
            if not norm_text(v.get("value")):
                r3 += 1
            if a and norm_text(v.get("value")) != norm_text(a["a"]):
                r2 += 1
        else:
            if not v.get("picked"):
                r3 += 1
            if a:
                want = set()
                for ch in letters_of(a["a"]):
                    k = LETTERS.index(ch)
                    if k < len(p["opts"]):
                        want.add(norm_text(p["opts"][k]))
                got = set(norm_text(x) for x in v.get("picked", []))
                if want != got:
                    r1 += 1

    print(f"  ①选择题文本匹配: {'PASS' if r1 == 0 else f'FAIL {r1} 处'}")
    print(f"  ②填空用原文    : {'PASS' if r2 == 0 else f'FAIL {r2} 处'}")
    print(f"  ③回读无空值    : {'PASS' if r3 == 0 else f'FAIL {r3} 处'}")

    if r1 or r2 or r3:
        bad("门禁未通过 —— 浏览器保持打开，请人工处理")
        return 5

    # 等交卷
    print("\n" + "=" * 62)
    print("✅ 门禁全部通过，100 题已填")
    print("   👉 请在浏览器里复核后点「交卷」")
    print(f"   本脚本会等待交卷完成（最多 {SUBMIT_WAIT_S//60} 分钟）并自动回捞")
    print("=" * 62, flush=True)

    t0 = time.time()
    while time.time() - t0 < SUBMIT_WAIT_S:
        time.sleep(10)
        st2 = get_state(s)
        if st2["status"] == 4:
            ok(f"检测到已交卷，得分 {st2['score']}")
            break
        el = int(time.time() - t0)
        if el % 60 < 11:
            info(f"等待交卷... {el}s")
    else:
        warn("等待超时，未检测到交卷")
        return 6

    # 回捞
    step("回捞 + 冲突检查")
    r = subprocess.run([sys.executable, os.path.join(HERE, "qbank.py"), "harvest", eid],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if r.stdout:
        print(r.stdout.strip())
    if r.stderr:
        print(r.stderr.strip())
    r2p = subprocess.run([sys.executable, os.path.join(HERE, "qbank.py"), "conflicts"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    print("\n冲突检查:")
    print((r2p.stdout or "").strip())

    try:
        ctx.close(); pw.stop()
    except Exception:
        pass
    return 0


def stage_harvest(s):
    step("回捞 + 冲突检查")
    st = get_state(s)
    if st["status"] != 4:
        bad(f"考试未完成 (status={st['status']})")
        return 2
    eid = str(st["examuserId"])
    info(f"examuserId={eid}, score={st['score']}")
    r = subprocess.run([sys.executable, os.path.join(HERE, "qbank.py"), "harvest", eid],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if r.stdout:
        print(r.stdout.strip())
    if r.stderr:
        print(r.stderr.strip())
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["auto", "fill", "state", "harvest"])
    a = ap.parse_args()
    s = load_session()

    if a.stage == "state":
        print(json.dumps(get_state(s), ensure_ascii=False, indent=1))
        return 0
    if a.stage == "auto":
        return stage_auto(s)
    if a.stage == "fill":
        return do_fill(s)
    return stage_harvest(s)


if __name__ == "__main__":
    sys.exit(main())
