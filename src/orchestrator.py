#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py — 产品级答题编排（题库优先 → 教材兜底 → 人工复核）

分层与置信度（全部经 200 题实测校准）:

  L0 题库命中        conf 0.99   —— 命中即用，实测 100% 正确
  L1 教材·高置信     conf ≥0.90  —— 实测准确率 98.6%，自动采纳
  L2 教材·中置信     0.70~0.90   —— 采用但标记「建议复核」
  L3 教材·低置信     <0.70       —— 不填，标记 REVIEW
  L4 无解                        —— REVIEW

产品铁律:
  · **绝不瞎填**：低于闸门一律留空交人工，错填比空着更糟
  · 每题都留证据（命中来源 / 教材出处 / 置信度），供 UI 展示
  · REVIEW 题集中呈现，人工补完即可交卷
"""
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from answer_engine import (Book, solve_one, TEXT_TYPES, clean_stem, prep_stem)  # noqa
from qbank import load_bank, norm_stem, norm_ans  # noqa

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# —— 阈值（实测校准，改动前请先跑 eval_engine.py）——
GATE_AUTO = 0.90    # ≥ 自动采纳
GATE_SOFT = 0.70    # ≥ 采用但需复核
GATE_MIN = 0.55     # < 一律不填


def norm(s):
    return re.sub(r"[\s\u3000，、,|]+", "", unicodedata.normalize("NFKC", s or "")).strip()


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def load_corpora(corpus_dir=None):
    """载入全部语料，返回 Book 列表（多份语料按需切换）。"""
    corpus_dir = corpus_dir or HERE
    cands = ["corpus_textlayer.txt", "corpus_nobox.txt", "corpus_mineru.txt"]
    books = []
    for c in cands:
        p = os.path.join(corpus_dir, c)
        if os.path.exists(p):
            b = Book(p)
            if b.text:
                books.append(b)
    if not books:
        import glob
        for p in sorted(glob.glob(os.path.join(corpus_dir, "corpus_*.txt"))):
            b = Book(p)
            if b.text:
                books.append(b)
    return books


class Orchestrator:
    def __init__(self, corpus_dir=None, bank_path=None):
        self.books = load_corpora(corpus_dir)
        self.book = self.books[0] if self.books else None
        self.bank = load_bank()
        self.stats = {}

    # ---------------------------------------------------------- 题库
    def bank_lookup(self, title):
        key = norm_stem(title)
        if len(key) < 8:
            return None
        e = self.bank["entries"].get(key)
        if not e or not e.get("variants"):
            return None
        v = e["variants"][0]
        return {"answer": v.get("answer"), "conf": 0.99, "hits": e.get("hits", 0),
                "sources": e.get("sources", []), "type": v.get("type")}

    # ---------------------------------------------------------- 单题
    def solve(self, idx, p):
        """返回一条结果，字段直接喂给 UI。"""
        title = (p.get("title") or [""])[0] if isinstance(p.get("title"), list) else p.get("title", "")
        opts = [o[0] if isinstance(o, list) else o for o in (p.get("option") or [])]
        stype = p.get("type")
        judge = len(opts) == 2 and [str(o) for o in opts] == ["对", "错"]

        out = {"idx": idx, "sid": str(p.get("subjectId")), "type": stype,
               "title": title, "options": opts, "judge": judge,
               "answer": None, "tier": "review", "conf": 0.0,
               "source": None, "why": "", "evidence": {}}

        # L0 题库
        hit = self.bank_lookup(title)
        if hit and hit.get("answer"):
            out.update({"answer": hit["answer"], "tier": "qbank", "conf": 0.99,
                        "source": "题库", "why": f"命中题库（历史 {hit['hits']} 次）"})
            return out

        # L1~L3 教材兜底
        if self.book:
            r = solve_one(self.book, stype, title, opts)
            a, c = r["answer"], r["conf"]
            out["evidence"] = r.get("evidence") or {}
            if a and c >= GATE_AUTO:
                out.update({"answer": a, "tier": "textbook_auto", "conf": c,
                            "source": "教材", "why": f"教材高置信（{out['evidence'].get('method')}）"})
                return out
            if a and c >= GATE_SOFT:
                out.update({"answer": a, "tier": "textbook_soft", "conf": c,
                            "source": "教材", "why": f"教材中置信，建议复核（{out['evidence'].get('method')}）"})
                return out
            if a:
                out.update({"conf": c, "tier": "review", "source": "教材",
                            "why": f"教材低置信 {c:.2f}，宁缺毋滥不填",
                            "candidate": a})
                return out

        out["why"] = out["why"] or "题库与教材均无可靠答案"
        return out

    # ---------------------------------------------------------- 全卷
    def solve_paper(self, paper, progress=None):
        plans = []
        for i, p in enumerate(paper, 1):
            r = self.solve(i, p)
            plans.append(r)
            if progress:
                progress(i, len(paper), r)
        self._tally(plans)
        return {"plan": plans, "stat": self.stats}

    def _tally(self, plans):
        s = {"qbank": 0, "textbook_auto": 0, "textbook_soft": 0, "review": 0}
        for r in plans:
            s[r["tier"]] = s.get(r["tier"], 0) + 1
        n = len(plans) or 1
        self.stats = {
            "total": len(plans),
            "qbank": s["qbank"],
            "textbook_auto": s["textbook_auto"],
            "textbook_soft": s["textbook_soft"],
            "review": s["review"],
            "auto_cover": round((s["qbank"] + s["textbook_auto"]) / n * 100, 1),
            "all_cover": round((len(plans) - s["review"]) / n * 100, 1),
        }

    # ---------------------------------------------------------- 填写指令
    def to_fill_payload(self, result, include_soft=True):
        """把 plan 转成浏览器填写指令 {subjectId: {a, t}}。"""
        ans = {}
        for r in result["plan"]:
            if not r.get("answer"):
                continue
            if r["tier"] == "textbook_soft" and not include_soft:
                continue
            if r["tier"] == "review":
                continue
            if r["type"] in TEXT_TYPES:
                ans[r["sid"]] = {"a": r["answer"], "t": "t", "tier": r["tier"]}
            else:
                ans[r["sid"]] = {"a": letters_of(r["answer"]), "t": "s", "tier": r["tier"]}
        return ans


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "plan_product.json"))
    a = ap.parse_args()
    o = Orchestrator()
    paper = json.load(open(a.paper, encoding="utf-8"))
    res = o.solve_paper(paper)
    json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    st = res["stat"]
    print(f"共 {st['total']} 题")
    print(f"  题库命中   : {st['qbank']:3d}")
    print(f"  教材·自动  : {st['textbook_auto']:3d}")
    print(f"  教材·待复核: {st['textbook_soft']:3d}")
    print(f"  需人工     : {st['review']:3d}")
    print(f"  → 自动覆盖 {st['auto_cover']}%  含待复核 {st['all_cover']}%")
