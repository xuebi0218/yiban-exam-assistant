#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_eval_set.py — 构建多套评测集 (paper + truth)，用于避免单卷过拟合

已有的交卷快照:
  after_submit.json     / attempt4_final.json / attempt5_export.json
  answer_truth_6.json   (本局)

输出到 data/evalset/ :
  setN_paper.json   试卷 (subjectId/type/title/option)
  setN_truth.json   {sid: 明文答案}
  manifest.json     索引
"""
import json
import os
import sys
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "evalset")
LETTERS = "ABCDE"


def letters_of(a):
    return "".join(sorted(set(re.findall(r"[A-Z]", (a or "").upper()))))


def _load(p):
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def load_raw_pairs():
    """返回 [(name, subjects[], truth_loader)]；subject 含 title/type/option/answer密文 或明文。"""
    sets = []
    # 1) after_submit.json —— 里面已含解密后答案? 检查字段
    p = os.path.join(HERE, "after_submit.json")
    if os.path.exists(p):
        sets.append(("set1_after_submit", _load(p)))
    # 2) attempt5_export / attempt4_final / attempt3
    for name, fn in [("set2_attempt5", "attempt5_export.json"),
                     ("set3_attempt4", "attempt4_final.json")]:
        p = os.path.join(HERE, fn)
        if os.path.exists(p):
            sets.append((name, _load(p)))
    return sets


def extract(d):
    """从各种历史快照里抽出 subjects 列表。"""
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for k in ("answers", "subjects", "paper", "data"):
        v = d.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if "subjects" in v[0]:
                return [x for sec in v for x in sec["subjects"]]
            return v
        if isinstance(v, dict):
            r = extract(v)
            if r:
                return r
    return []


def normalize_subject(s):
    """统一成 {subjectId,type,title,option,plain?,cipher?}"""
    opts = s.get("option") or s.get("options") or []
    opts = [o[0] if isinstance(o, list) else o for o in opts]
    # plain 优先级: answer(明文) > plain > disp > decrypted
    plain = s.get("answer") or s.get("plain") or s.get("disp") or s.get("decrypted")
    cipher = s.get("cipher") or s.get("answer_cipher")
    # 若 answer 看起来是 base64 密文(短且含 = / +), 视为密文
    if isinstance(plain, str) and re.fullmatch(r"[A-Za-z0-9+/=]{16,}", plain or ""):
        cipher, plain = plain, None
    return {
        "subjectId": s.get("subjectId") or s.get("id"),
        "type": s.get("type"),
        "title": s.get("title") or s.get("stem") or "",
        "option": opts,
        "cipher": cipher,
        "plain": plain,
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = []
    for name, d in load_raw_pairs():
        subs = extract(d)
        if not subs:
            print(f"[skip] {name}: 无 subjects")
            continue
        paper, truth = [], {}
        for s0 in subs:
            s = normalize_subject(s0)
            if not s["subjectId"]:
                continue
            paper.append({"subjectId": s["subjectId"], "type": s["type"],
                          "title": [s["title"]] if isinstance(s["title"], str) else s["title"],
                          "option": [[o] for o in s["option"]],
                          "answer": s["cipher"]})
            if s["plain"]:
                truth[str(s["subjectId"])] = s["plain"]
        pp = os.path.join(OUT, name + "_paper.json")
        json.dump(paper, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        tp = None
        if truth:
            tp = os.path.join(OUT, name + "_truth.json")
            json.dump(truth, open(tp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        manifest.append({"name": name, "paper": os.path.basename(pp),
                         "truth": os.path.basename(tp) if tp else None,
                         "n": len(paper), "n_truth": len(truth)})
        print(f"{name}: paper {len(paper)} 题, truth {len(truth)} 条")

    # 本局
    p6 = os.path.join(HERE, "live_paper_auto.json")
    t6 = os.path.join(HERE, "answer_truth_6.json")
    if os.path.exists(p6) and os.path.exists(t6):
        import shutil
        shutil.copy(p6, os.path.join(OUT, "set4_round6_paper.json"))
        shutil.copy(t6, os.path.join(OUT, "set4_round6_truth.json"))
        n = len(json.load(open(p6, encoding="utf-8")))
        manifest.append({"name": "set4_round6",
                         "paper": "set4_round6_paper.json",
                         "truth": "set4_round6_truth.json",
                         "n": n, "n_truth": 100})
        print(f"set4_round6: paper {n} 题, truth(raw) 100 条")

    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n评测集目录: {OUT}")
    print(f"套数: {len(manifest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
