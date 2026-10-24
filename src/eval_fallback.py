#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_fallback.py — 评测「题库未命中时的教材兜底」真实准确率

输入:
  --paper   试卷(json, 含 subjectId/type/title/option)
  --truth   真答案(json, 交卷后 answer/get 原始返回, 或 {sid: disp} 映射)
  --corpus  教材语料 txt

输出:
  分题型统计 textbook 求解的 准确率 / 覆盖率 / 错例明细

用法:
  python src/eval_fallback.py --paper data/live_paper_auto.json \
      --truth data/answer_truth_6.json --corpus data/corpus_textlayer.txt
"""
import argparse
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def norm(s):
    return re.sub(r"[\s\u3000，、,|]+", "", unicodedata.normalize("NFKC", s or "")).strip()


def load_truth(path, yiban_id):
    """支持两种输入: 原始 answer/get payload, 或 {sid: 明文} 映射。"""
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, list):
        d = {"data": d}
    if isinstance(d.get("data"), list) and d["data"] and isinstance(d["data"][0], dict) \
            and "subjects" in d["data"][0]:
        from yooc_answers import decrypt_raw, to_display
        out = {}
        for sec in d["data"]:
            for s in sec["subjects"]:
                opts = [o[0] if isinstance(o, list) else o for o in (s.get("option") or [])]
                try:
                    vals = json.loads(decrypt_raw(str(s["answer"]), yiban_id))
                    disp, probs = to_display(vals, s.get("type"), n_options=(len(opts) or None))
                    if not probs:
                        out[str(s["subjectId"])] = {"disp": disp, "type": s.get("type"),
                                                    "options": opts}
                except Exception:
                    pass
        return out
    return {str(k): {"disp": v} for k, v in d.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--yiban", default="", help="你的 yiban_id（用于解密答案）")
    ap.add_argument("--no-qbank", action="store_true", default=True,
                    help="强制跳过题库，纯测教材兜底（默认开启）")
    ap.add_argument("--out", default=os.path.join(HERE, "eval_fallback.json"))
    a = ap.parse_args()

    from textbook_verify import Book
    from boundary_solver import solve_completion, solve_multi

    paper = json.load(open(a.paper, encoding="utf-8"))
    truth = load_truth(a.truth, a.yiban)
    book = Book(a.corpus)

    print(f"教材语料: {os.path.basename(a.corpus)}  ({len(book.text)} 字)")
    print(f"试卷 {len(paper)} 题 | 真答案 {len(truth)} 条")
    print("=" * 66)

    stats = {}
    rows = []
    for i, p in enumerate(paper, 1):
        sid = str(p.get("subjectId"))
        t = truth.get(sid)
        if not t:
            continue
        stype = p.get("type")
        title = (p.get("title") or [""])[0]
        opts = [o[0] if isinstance(o, list) else o for o in (p.get("option") or [])]
        gold = t["disp"]

        if stype == "completion":
            got, conf, method = solve_completion(book, title)
            ok = got is not None and norm(got) == norm(gold)
        else:
            got, conf, method = solve_multi(book, title, opts, stype)
            ok = got is not None and letters_of(got) == letters_of(gold)

        k = (stype, "hit" if got else "miss")
        s = stats.setdefault(stype, {"n": 0, "answered": 0, "ok": 0})
        s["n"] += 1
        if got:
            s["answered"] += 1
        if ok:
            s["ok"] += 1
        rows.append({"idx": i, "type": stype, "gold": gold, "got": got,
                     "conf": round(conf or 0, 2), "method": method, "ok": ok})

    print(f"{'题型':<16}{'题数':>6}{'作答':>6}{'正确':>6}{'作答准确率':>12}{'全题准确率':>12}")
    print("-" * 66)
    tot = {"n": 0, "answered": 0, "ok": 0}
    for stype, s in stats.items():
        prec = s["ok"] / s["answered"] * 100 if s["answered"] else 0
        rec = s["ok"] / s["n"] * 100
        print(f"{stype:<16}{s['n']:>6}{s['answered']:>6}{s['ok']:>6}"
              f"{prec:>11.1f}%{rec:>11.1f}%")
        for kk in tot:
            tot[kk] += s[kk]
    print("-" * 66)
    prec = tot["ok"] / tot["answered"] * 100 if tot["answered"] else 0
    rec = tot["ok"] / tot["n"] * 100
    print(f"{'合计':<16}{tot['n']:>6}{tot['answered']:>6}{tot['ok']:>6}"
          f"{prec:>11.1f}%{rec:>11.1f}%")

    # 置信度分档准确率（决定"自动采纳阈值"）
    print("\n置信度分档（决定自动采纳阈值）:")
    print(f"{'区间':<14}{'题数':>6}{'正确':>6}{'准确率':>10}")
    print("-" * 40)
    buckets = [(0, 0.55), (0.55, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in buckets:
        sel = [r for r in rows if r["got"] and lo <= r["conf"] < hi]
        if not sel:
            continue
        ok = sum(1 for r in sel if r["ok"])
        print(f"{f'{lo:.2f}~{hi:.2f}':<14}{len(sel):>6}{ok:>6}{ok/len(sel)*100:>9.1f}%")

    print("\n错例（作答但错）:")
    bad = [r for r in rows if r["got"] and not r["ok"]]
    for r in bad[:15]:
        print(f"  [{r['idx']:3d}] {r['type']:<12} conf={r['conf']:.2f} {r['method']:<10}")
        print(f"        我={str(r['got'])[:60]}")
        print(f"        正={str(r['gold'])[:60]}")
    if len(bad) > 15:
        print(f"  ... 共 {len(bad)} 处错例")

    json.dump({"corpus": a.corpus, "stats": stats, "rows": rows},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n明细已写出: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
