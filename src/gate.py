#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate.py — 交卷前的三道门禁（第 3 局 97 分复盘得出的硬性约束）

第 3 局失分 3 处（57/87/82）全部源于"填写层"而非"答案层"。
本脚本把三条规则做成可执行检查，交卷前必须全部通过。

用法：
  python src/gate.py --paper data/live_paper4.json \
                      --answers data/ans_example.json \
                      --readback data/readback4.json

  readback4.json 由浏览器导出，格式：
    { "1": {"kind":"text","value":"..."},
      "9": {"kind":"sel","order":["..."],"picked":["..."]}, ... }

退出码：0 全通过；1 有违规
"""
import argparse
import json
import re
import sys
import unicodedata

LETTERS = "ABCDE"


def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"^[A-Z][.、]\s*", "", s)
    return re.sub(r"[\s\u3000]+", "", s).strip()


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def check_rule1(paper, answers, readback):
    """规则① 选择题按文本匹配，禁字母索引。

    校验方式：把答案字母经 paper 选项顺序转成"应选文本集合"，
    与页面实际选中文本集合精确比对（不用子串）。
    """
    bad = []
    for i, p in enumerate(paper, 1):
        if p["type"] == "completion":
            continue
        a = answers.get(str(p["subjectId"]))
        rb = readback.get(str(i))
        if not a or not rb or rb.get("kind") != "sel":
            continue
        opts = [x[0] if isinstance(x, list) else x for x in (p.get("option") or [])]
        want = set()
        for ch in letters_of(a.get("a", "")):
            k = LETTERS.index(ch)
            if k < len(opts):
                want.add(norm(opts[k]))
        got = set(norm(t) for t in rb.get("picked", []))
        if want != got:
            bad.append({
                "idx": i, "rule": "①选择题文本匹配",
                "should": sorted(t[:24] for t in want),
                "actual": sorted(t[:24] for t in got),
                "extra": sorted(t[:24] for t in got - want),
                "missing": sorted(t[:24] for t in want - got),
            })
    return bad


def check_rule2(paper, answers, readback):
    """规则② 填空题必须用题库原文，禁简写。"""
    bad = []
    for i, p in enumerate(paper, 1):
        if p["type"] != "completion":
            continue
        a = answers.get(str(p["subjectId"]))
        rb = readback.get(str(i))
        if not a or not rb:
            continue
        want = norm(a.get("a", ""))
        got = norm(rb.get("value", ""))
        if want != got:
            bad.append({
                "idx": i, "rule": "②填空原文",
                "should": a.get("a", ""),
                "actual": rb.get("value", ""),
            })
    return bad


def check_rule3(paper, readback):
    """规则③ 回读校验：不能有空值。"""
    bad = []
    for i in range(1, len(paper) + 1):
        rb = readback.get(str(i))
        if not rb:
            bad.append({"idx": i, "rule": "③回读", "problem": "未回读"})
            continue
        if rb.get("kind") == "text":
            if not norm(rb.get("value", "")):
                bad.append({"idx": i, "rule": "③回读", "problem": "填空为空"})
        else:
            if not rb.get("picked"):
                bad.append({"idx": i, "rule": "③回读", "problem": "选择题未选"})
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True)
    ap.add_argument("--answers", required=True, help="subjectId -> {a,t}")
    ap.add_argument("--readback", required=True)
    args = ap.parse_args()

    paper = json.load(open(args.paper, encoding="utf-8"))
    answers = json.load(open(args.answers, encoding="utf-8-sig"))
    readback = json.load(open(args.readback, encoding="utf-8-sig"))

    print("=" * 62)
    print("交卷门禁检查")
    print("=" * 62)
    print(f"试卷 {len(paper)} 题 | 答案 {len(answers)} 条 | 回读 {len(readback)} 条\n")

    r1 = check_rule1(paper, answers, readback)
    r2 = check_rule2(paper, answers, readback)
    r3 = check_rule3(paper, readback)

    for name, bad in [("①选择题按文本匹配", r1), ("②填空用原文", r2), ("③回读无空值", r3)]:
        if bad:
            print(f"[FAIL] {name}: {len(bad)} 处")
            for b in bad[:10]:
                print(f"   第{b['idx']}题 {b}")
        else:
            print(f"[PASS] {name}")

    total = len(r1) + len(r2) + len(r3)
    print("\n" + "=" * 62)
    if total:
        print(f"未通过：共 {total} 处，禁止交卷")
        return 1
    print("全部通过，可以交卷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
