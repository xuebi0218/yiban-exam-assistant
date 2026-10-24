#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_engine.py — 评测 answer_engine v2 vs v1 在多套试卷上的表现

用法:
  python src/eval_engine.py --sets data/evalset --corpus data/corpus_textlayer.txt
  python src/eval_engine.py --sets data/evalset --corpus data/corpus_textlayer.txt --compare
"""
import argparse
import glob
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LETTERS = "ABCEDFGHIJKLMNOPQRSTUVWXYZ"


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def norm(s):
    return re.sub(r"[\s\u3000，、,|]+", "", unicodedata.normalize("NFKC", s or "")).strip()


def eval_set(book, paper_path, truth_path, engine="v2", books=None):
    paper = json.load(open(paper_path, encoding="utf-8"))
    truth = json.load(open(truth_path, encoding="utf-8"))

    from answer_engine import solve_one, consensus_solve, TEXT_TYPES
    if engine == "v1":
        from boundary_solver import solve_completion, solve_multi

    rows = []
    for i, p in enumerate(paper, 1):
        sid = str(p.get("subjectId"))
        gold = truth.get(sid)
        if gold is None:
            continue
        title = (p.get("title") or [""])[0] if isinstance(p.get("title"), list) else p.get("title", "")
        opts = [o[0] if isinstance(o, list) else o for o in (p.get("option") or [])]
        stype = p.get("type")

        if engine == "v1":
            if stype in ("completion",):
                got, conf, method = solve_completion(book, title)
            else:
                got, conf, method = solve_multi(book, title, opts, stype)
            ev = {}
        elif engine == "consensus" and books:
            r = consensus_solve(books, stype, title, opts)
            got, conf, ev = r["answer"], r["conf"], r["evidence"]
            # 投票只在**一致时才采信**：分歧即视为不可靠
            if isinstance(ev, dict) and ev.get("agree", 1.0) < 1.0:
                conf = min(conf, 0.69)
        else:
            r = solve_one(book, stype, title, opts)
            got, conf, ev = r["answer"], r["conf"], r["evidence"]

        if stype in ("completion",):
            ok = got is not None and norm(got) == norm(gold)
        else:
            ok = got is not None and letters_of(got) == letters_of(gold)

        rows.append({"idx": i, "sid": sid, "type": stype, "gold": gold, "got": got,
                     "conf": conf or 0, "ok": bool(ok),
                     "method": (ev.get("method") if isinstance(ev, dict) else method)})
    return rows


def summarize(rows, label=""):
    stats = {}
    for r in rows:
        s = stats.setdefault(r["type"], {"n": 0, "ans": 0, "ok": 0})
        s["n"] += 1
        if r["got"]:
            s["ans"] += 1
        if r["ok"]:
            s["ok"] += 1
    tot = {"n": 0, "ans": 0, "ok": 0}
    for s in stats.values():
        for k in tot:
            tot[k] += s[k]
    prec = tot["ok"] / tot["ans"] * 100 if tot["ans"] else 0
    print(f"\n{label}")
    print(f"  {'题型':<15}{'题数':>6}{'作答':>6}{'正确':>6}{'作答准确率':>12}")
    for t, s in sorted(stats.items()):
        p = s["ok"] / s["ans"] * 100 if s["ans"] else 0
        print(f"  {t:<15}{s['n']:>6}{s['ans']:>6}{s['ok']:>6}{p:>11.1f}%")
    print(f"  {'合计':<15}{tot['n']:>6}{tot['ans']:>6}{tot['ok']:>6}{prec:>11.1f}%")
    return tot, stats, prec


def conf_report(rows, threshold=0.70):
    """按置信度阈值，看自动采纳后的准确率与覆盖率。"""
    print(f"\n  置信度阈值效应（关键：产品化要保证自动采纳的准确性）:")
    print(f"  {'阈值':<8}{'采纳数':>7}{'采纳中正确':>11}{'准确率':>9}{'覆盖率':>8}")
    for th in (0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        sel = [r for r in rows if r["got"] and r["conf"] >= th]
        if not sel:
            continue
        ok = sum(1 for r in sel if r["ok"])
        print(f"  {th:<8.2f}{len(sel):>7}{ok:>11}{ok/len(sel)*100:>8.1f}%"
              f"{len(sel)/len(rows)*100:>7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default=os.path.join(HERE, "evalset"))
    ap.add_argument("--corpus", default=None,
                    help="单语料；缺省时自动联合所有 corpus_*.txt")
    ap.add_argument("--compare", action="store_true", help="同时跑 v1 对比")
    ap.add_argument("--mode", choices=["single", "consensus"], default="consensus",
                    help="single=单语料; consensus=多语料投票(默认)")
    ap.add_argument("--show-bad", type=int, default=6)
    a = ap.parse_args()

    from answer_engine import Book
    if a.mode == "consensus":
        files = ([a.corpus] if isinstance(a.corpus, str) else a.corpus) or \
                sorted(glob.glob(os.path.join(HERE, "corpus_*.txt")))
        books = [Book(f) for f in files]
        books = [b for b in books if b.text]
        book = books[0]
        print(f"语料: {len(books)} 份联合投票 -> {', '.join(os.path.basename(str(f)) for f in files)}")
    else:
        corpus = a.corpus or os.path.join(HERE, "corpus_textlayer.txt")
        book = Book(corpus)
        books = None
        print(f"语料: {os.path.basename(str(corpus))}  ({len(book.text)} 字)")

    pairs = []
    for pf in sorted(glob.glob(os.path.join(a.sets, "*_paper.json"))):
        tf = pf.replace("_paper.json", "_truth.json")
        if os.path.exists(tf):
            pairs.append((os.path.basename(pf).replace("_paper.json", ""), pf, tf))
    if not pairs:
        print("未找到评测集")
        return 1

    allv2 = []
    for name, pf, tf in pairs:
        print("\n" + "=" * 68)
        print(f"答卷: {name}")
        print("=" * 68)
        r2 = eval_set(book, pf, tf, "v2")
        allv2 += r2
        tot, stats, prec = summarize(r2, "answer_engine v2:")
        conf_report(r2)
        if a.compare:
            r1 = eval_set(book, pf, tf, "v1")
            tot1, _, prec1 = summarize(r1, "boundary_solver v1:")
            print(f"\n  >>> v1 {prec1:.1f}%  →  v2 {prec:.1f}%  "
                  f"({'提升' if prec > prec1 else '下降'} {abs(prec-prec1):.1f}pp)")
        bad = [r for r in r2 if r["got"] and not r["ok"]]
        if bad and a.show_bad:
            print(f"\n  错例 (共 {len(bad)}):")
            for r in bad[:a.show_bad]:
                print(f"    [{r['idx']:3d}] {r['type']:<14} conf={r['conf']:.2f} {r.get('method')}")
                print(f"          我={str(r['got'])[:56]}")
                print(f"          正={str(r['gold'])[:56]}")

    print("\n" + "#" * 68)
    print("# 全答卷汇总 (v2)")
    print("#" * 68)
    summarize(allv2, "ALL:")
    conf_report(allv2)
    json.dump(allv2, open(os.path.join(HERE, "eval_engine_v2.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细: data/eval_engine_v2.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
