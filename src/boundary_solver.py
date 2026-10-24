#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
boundary_solver.py — 精确边界求解器（针对实测 12 个错例改进）

核心改进（对应 4 类根因）:

A. 边界对齐（题 13/52/64 截断过短, 题 5/14 过短或偏差）
   填空题的答案边界必须由「题干 prefix 的实际末尾」与「suffix 的实际开头」
   共同锚定。做法: 在语料中定位 suffix, 再向左对齐 prefix 的最长可匹配尾部,
   两者之间的文本即为答案, 不额外截断、不额外扩展。

B. 整句优先（题 5: 正确答案与题干共享较长前缀）
   若题干 prefix 很长, 必须使用最长匹配, 而非固定长度锚点。

C. 多选完备性（题 28/50/69 漏选）
   多选题的判定阈值不能一刀切。原文中若某选项被完整覆盖, 就必须选;
   仅在选项明显不属该段（背景距离过大）时排除。

D. 标点与虚词归一（题 34「系统整合」vs「进行系统集成」）
   这类是题库固定表述, 只能靠更精确的原文定位解决, 而非模糊匹配。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from textbook_verify import Book, normalize, prep_stem  # noqa: E402

SENT = "\uE000"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def solve_completion(book, title):
    """返回 (answer, confidence, method)。

    以 suffix 为主锚点, 用 prefix 对齐左边界, 保证答案既不过短也不过长。
    """
    pre, suf = prep_stem(title)
    pre = pre or ""
    suf = suf or ""

    # 主策略: suffix 定位 + prefix 左对齐
    if len(suf) >= 4:
        for sl in (14, 12, 10, 8, 6, 5, 4):
            if len(suf) < sl:
                continue
            head = suf[:sl]
            for pos in book.occurrences(head, cap=40):
                left_ctx = book.text[max(0, pos - 70):pos]
                # 用最长 prefix 尾部对齐左边界
                start = None
                best_pl = 0
                for pl in (26, 22, 18, 15, 12, 10, 8, 6):
                    if len(pre) < pl:
                        continue
                    k = left_ctx.rfind(pre[-pl:])
                    if k >= 0:
                        start = k + pl
                        best_pl = pl
                        break
                if start is None:
                    continue
                cand = left_ctx[start:].strip()
                if not cand or len(cand) > 50:
                    continue
                if not re.search(r"[\u4e00-\u9fa5]", cand):
                    continue
                # 置信度: 对齐越长越可信
                conf = 0.95 if best_pl >= 15 else (0.85 if best_pl >= 10 else 0.7)
                return cand, conf, f"suf+pre{best_pl}"

    # 次策略: prefix 定位 + 截到 suffix 或句读
    if len(pre) >= 6:
        for pl in (20, 16, 12, 10, 8, 6):
            if len(pre) < pl:
                continue
            tail = pre[-pl:]
            for pos in book.occurrences(tail, cap=40):
                right = book.text[pos + pl: pos + pl + 70]
                # 优先用 suffix 截断
                end = None
                if len(suf) >= 4:
                    for sl in (8, 6, 5, 4):
                        if len(suf) < sl:
                            continue
                        j = right.find(suf[:sl])
                        if j > 0:
                            end = j
                            break
                if end is None:
                    j = min((right.find(c) for c in "。;" if c in right), default=-1)
                    end = j if j > 0 else min(30, len(right))
                cand = right[:end].strip()
                if 1 <= len(cand) <= 50 and re.search(r"[\u4e00-\u9fa5]", cand):
                    return cand, (0.8 if pl >= 12 else 0.65), f"pre{pl}"
    return None, 0.0, "miss"


def solve_multi(book, title, options, stype):
    """多选/单选/判断: 基于原文段落覆盖率, 完备性优先。"""
    stem = normalize(re.sub(r"[（(]\s*[)）]", "", re.sub(r"<[^>]+>", "", title)))
    stem = re.sub(r"\{input\}", "", stem)

    pos = -1
    for n in (30, 26, 22, 18, 14):
        if len(stem) < n:
            continue
        p = book.text.find(stem[:n])
        if p >= 0:
            pos = p
            break
    if pos < 0:
        for n in (16, 12, 8):
            if len(stem) < n:
                continue
            mid = stem[len(stem) // 3: len(stem) // 3 + n]
            p = book.text.find(mid)
            if p >= 0:
                pos = max(0, p - len(stem) // 3)
                break

    judge = [str(o) for o in options[:2]] == ["对", "错"]
    if judge:
        stmt = stem.replace("()", "")
        hit = len(stmt) >= 12 and stmt[:26] in book.text
        return ("A" if hit else "B"), (0.75 if hit else 0.4), "judge"

    if pos < 0:
        return None, 0.0, "nostem"

    passage = book.text[pos: pos + 620]
    scores = {}
    for idx, o in enumerate(options):
        on = normalize(o)
        if not on:
            scores[idx] = 0.0
            continue
        if on in passage:
            scores[idx] = 1.0
            continue
        # 最长公共子串比例
        best = 0
        for L in range(min(len(on), 26), 4, -1):
            if any(on[i:i + L] in passage for i in range(0, len(on) - L + 1)):
                best = L / len(on)
                break
        scores[idx] = best

    if stype == "choice":
        idx = max(scores, key=lambda k: scores[k])
        return LETTERS[idx], round(scores[idx], 2), "single"

    # 多选: 阈值放宽到 0.55, 且强制至少 1 项; 完整性优先于精确性
    sel = [i for i in sorted(scores) if scores[i] >= 0.55]
    if not sel:
        sel = [max(scores, key=lambda k: scores[k])]
    return "、".join(LETTERS[i] for i in sel), round(sum(scores[i] for i in sel) / len(sel), 2), "multi"


def main():
    paper = json.load(open(os.path.join(HERE, "live_paper.json"), encoding="utf-8"))
    book = Book(os.path.join(HERE, "corpus.txt"))
    out = {}
    for i, s in enumerate(paper, 1):
        t = (s.get("title") or [""])[0]
        opts = [o[0] if isinstance(o, list) else o for o in (s.get("option") or [])]
        if s["type"] == "completion":
            a, c, m = solve_completion(book, t)
        else:
            a, c, m = solve_multi(book, t, opts, s["type"])
        out[i] = {"answer": a, "conf": c, "method": m}
    json.dump(out, open(os.path.join(HERE, "solved_v3.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    from regression import score
    r = score({k: v["answer"] for k, v in out.items()})
    print(f"v3 正确 {r['ok']}/{r['total']} = {r['rate']}%")
    for b in r["bad"]:
        print(f"  [{b['idx']:3d}] 我={b['mine']}  正={b['correct']}")


if __name__ == "__main__":
    main()
