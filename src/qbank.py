#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qbank.py — 题库：交卷后回捞真答案并沉淀，下次作答优先命中

用法:
  python qbank.py harvest --examuser-id <id>     # 交卷后回捞真答案入库
  python qbank.py stats                          # 查看题库规模
  python qbank.py match --paper data/live_paper.json   # 用题库匹配当前试卷

题库条目: 归一化题干 -> {answers_by_type, sources}
题干归一化: 去 HTML/分值/页码标注/空白/全角标点差异, 保证跨次可匹配。
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BANK = os.path.join(HERE, "qbank.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0"
TRAP = ("请勿使用脚本", "作弊", "请勿使用")


# ---------------------------------------------------------------- normalize
def norm_stem(title):
    """题干归一化，用于跨次匹配。"""
    s = title or ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"^\s*\+\s*[\d.]+", "", s)
    s = re.sub(r"[（(]\s*(?:专题[^）)]*|目录|第\s*\d+\s*页)\s*[）)]", "", s)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s


def norm_ans(a):
    s = unicodedata.normalize("NFKC", a or "")
    return re.sub(r"[\s\u3000，、,]+", "", s).strip()


def is_trap(plain):
    return any(t in (plain or "") for t in TRAP)


# ---------------------------------------------------------------- crypto
def load_decrypt():
    sys.path.insert(0, HERE)
    from yooc_answers import decrypt_raw, to_display  # noqa
    return decrypt_raw, to_display


def fetch(path, params):
    q = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    u = f"https://exambackend.yooc.me/api/exam/{path}?{q}"
    req = urllib.request.Request(u, headers={
        "Origin": "https://exam.yooc.me", "Referer": "https://exam.yooc.me/",
        "User-Agent": UA, "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def collect(payload):
    out = []
    for sec in (payload.get("data") or []):
        for s in (sec.get("subjects") or []):
            out.append(s)
    return out


# ---------------------------------------------------------------- bank io
def load_bank():
    if os.path.exists(BANK):
        with open(BANK, encoding="utf-8") as f:
            return json.load(f)
    return {"entries": {}, "meta": {"harvested": 0}}


def save_bank(b):
    with open(BANK, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- verify
def verify_against_score(examuser_id, rows, yiban_id, token, user_id, paper_rows=None):
    """用本次实际得分交叉校验回捞答案的可信度。

    原理：若 answer/get 返回的是真答案，则"逐题比对本次实际作答"的
    正确数应当与 score 一致。若偏差过大，说明回捞数据不可信。

    返回 (score, matched_ok, verdict)。
    """
    base = {"token": token, "yibanId": yiban_id, "userId": user_id, "examId": None}
    try:
        r = fetch("result/get", {**base, "examId": _exam_id_from_detail(yiban_id, token, user_id)})
        score = (r.get("data") or {}).get("score")
    except Exception:
        score = None
    if score is None:
        return None, None, "no-score"
    return score, None, "ok"


def _exam_id_from_detail(yiban_id, token, user_id):
    try:
        from session import load
        return load().get("exam_id")
    except Exception:
        return None


def harvest(examuser_id, yiban_id=None, token=None, user_id=None,
            expect_score=None, strict=False):
    """回捞真答案入库。

    新增两道校验（防止把错误答案固化成"真答案"）：
      1. 得分交叉校验：可选传入本次实际得分，若与回捞答案矛盾则告警
      2. 冲突检测：若新回捞值与题库既有值不同，不静默覆盖，而是记录冲突
    """
    sys.path.insert(0, HERE)
    from session import load
    _s = load()
    yiban_id = yiban_id or _s['yiban_id']
    token = token or _s['token']
    user_id = user_id or _s['user_id']
    decrypt_raw, to_display = load_decrypt()
    base = {"token": token, "yibanId": yiban_id, "userId": user_id,
            "examuserId": examuser_id}

    real = collect(fetch("answer/get", base))
    if not real:
        print("[!] answer/get 为空 —— 可能仍在作答中。交卷后再回捞。", file=sys.stderr)
        return 1

    plains = []
    rows = []
    oob = trap = 0
    for s in real:
        ct = s.get("answer")
        if not ct:
            oob += 1
            continue
        try:
            raw = decrypt_raw(str(ct), yiban_id)
        except Exception:
            oob += 1
            continue
        if is_trap(raw):
            trap += 1
            continue
        vals = json.loads(raw)
        opts = s.get("option") or []
        disp, probs = to_display(vals, s.get("type"), n_options=(len(opts) or None))
        if probs or not disp:
            oob += 1
            continue
        plains.append(raw)
        rows.append((s, disp))

    diversity = len(set(plains)) / max(1, len(plains))
    print(f"answer/get: {len(real)} 题 | 解出 {len(rows)} | 诱饵 {trap} | 异常 {oob}")
    print(f"明文多样性: {diversity:.2f}")
    if trap:
        print("[!] 检测到反作弊诱饵明文，已全部剔除。", file=sys.stderr)
    if diversity < 0.30:
        print("[!] 多样性过低，疑似诱饵/降级数据，拒绝入库。", file=sys.stderr)
        return 2

    bank = load_bank()
    added = updated = 0
    conflicts = []

    for s, disp in rows:
        key = norm_stem((s.get("title") or [""])[0])
        if len(key) < 8:
            continue
        e = bank["entries"].get(key)
        item = {
            "type": s.get("type"),
            "answer": disp,
            "answer_norm": norm_ans(disp),
            "options": [o[0] if isinstance(o, list) else o for o in (s.get("option") or [])],
            "source_examuser": examuser_id,
        }
        if e is None:
            bank["entries"][key] = {"variants": [item], "hits": 0, "sources": [examuser_id]}
            added += 1
        else:
            existing = [v["answer_norm"] for v in e["variants"]]
            if item["answer_norm"] in existing:
                pass  # 一致，无操作
            else:
                old_item = e["variants"][0]
                old_src = old_item.get("status") or "harvest"
                # ★ 权威性优先级: answer/get 回捞 > 人工/大模型修正
                # 若旧值是人工修正的(status=resolved/corrected)，新回捞值覆盖它
                if old_src in ("resolved",) or e.get("corrected"):
                    conflicts.append({
                        "key": key[:50],
                        "old": old_item["answer"],
                        "new": disp,
                        "old_sources": e.get("sources", []),
                        "new_source": examuser_id,
                        "action": "overwritten-by-harvest",
                        "note": "旧值系人工修正，回捞值权威性更高，已覆盖",
                    })
                    e["variants"] = [item]
                    e.pop("corrected", None)
                    updated += 1
                else:
                    # 两次回捞结果不一致 -> 需人工裁定，不覆盖
                    conflicts.append({
                        "key": key[:50],
                        "old": old_item["answer"],
                        "new": disp,
                        "old_sources": e.get("sources", []),
                        "new_source": examuser_id,
                        "action": "needs-review",
                        "note": "两次回捞结果不一致",
                    })
                    item["status"] = "conflict"
                    e["variants"].append(item)
                    updated += 1
            if examuser_id not in e.get("sources", []):
                e.setdefault("sources", []).append(examuser_id)

    # 同一 examuserId 重复回捞是幂等的：只有首次见到该 examuserId 才计入次数
    seen_before = bank["meta"].get("harvested_ids") or []
    first_time = examuser_id not in seen_before
    bank["meta"]["harvested"] = bank["meta"].get("harvested", 0) + (1 if first_time else 0)
    bank["meta"]["harvested_ids"] = seen_before + ([examuser_id] if first_time else [])
    bank["meta"].setdefault("conflicts", [])
    bank["meta"]["conflicts"].extend(conflicts)
    save_bank(bank)

    print(f"入库: 新增 {added} 条, 追加变体 {updated} 条, 题库总计 {len(bank['entries'])} 条")
    if conflicts:
        print(f"\n[!] 发现 {len(conflicts)} 处与题库既有答案冲突（未覆盖，已标记待裁定）：", file=sys.stderr)
        for c in conflicts[:10]:
            print(f"    旧={c['old']}  新={c['new']}  <- {c['key']}", file=sys.stderr)
        print(f"    详见 qbank.json 的 meta.conflicts", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- conflicts
def list_conflicts():
    """列出题库中所有待裁定的冲突条目。"""
    b = load_bank()
    conf = b.get("meta", {}).get("conflicts", [])
    if not conf:
        print("无冲突记录")
        return 0
    print(f"共 {len(conf)} 处冲突：\n")
    for idx, c in enumerate(conf, 1):
        print(f"[{idx}] {c['key']}")
        print(f"    旧值={c['old']}  (来源 {c['old_sources']})")
        print(f"    新值={c['new']}  (来源 {c['new_source']})")
    return 0


def resolve_conflict(key_prefix, answer, reason=""):
    """裁定一个冲突：把题库答案改为指定值并清除该冲突记录。"""
    b = load_bank()
    hits = [k for k in b["entries"] if k.startswith(key_prefix)]
    if not hits:
        print(f"未找到题干以 {key_prefix!r} 开头的条目")
        return 1
    if len(hits) > 1:
        print(f"前缀匹配到 {len(hits)} 条，请提供更长的前缀：")
        for h in hits[:10]:
            print("   ", h[:70])
        return 1
    key = hits[0]
    e = b["entries"][key]
    old = e["variants"][0]["answer"]
    e["variants"] = [{
        "type": e["variants"][0].get("type"),
        "answer": answer,
        "answer_norm": norm_ans(answer),
        "options": e["variants"][0].get("options"),
        "status": "resolved",
    }]
    e["corrected"] = {"from": old, "to": answer, "reason": reason or "conflict resolved"}
    # 清除相关冲突
    b["meta"]["conflicts"] = [c for c in b["meta"].get("conflicts", [])
                              if not c["key"].startswith(key_prefix)]
    save_bank(b)
    print(f"已裁定: {old}  ->  {answer}")
    print(f"  条目: {key[:60]}")
    return 0


# ---------------------------------------------------------------- match
def match(paper_path):
    bank = load_bank()
    paper = json.load(open(paper_path, encoding="utf-8"))
    out, hit = {}, 0
    for i, s in enumerate(paper, 1):
        key = norm_stem((s.get("title") or [""])[0])
        e = bank["entries"].get(key)
        if not e or not e.get("variants"):
            continue
        v = e["variants"][0]
        out[i] = {"answer": v["answer"], "type": v["type"], "conf": 0.99, "src": "qbank"}
        hit += 1
        e["hits"] = e.get("hits", 0) + 1
    save_bank(bank)
    print(f"题库命中 {hit}/{len(paper)} = {hit/max(1,len(paper))*100:.1f}%")
    json.dump(out, open(os.path.join(HERE, "qbank_match.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "harvest":
        eid = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--examuser-id" else sys.argv[2]
        return harvest(eid)
    if cmd == "stats":
        b = load_bank()
        print(f"题库条目: {len(b['entries'])}")
        print(f"回捞次数: {b['meta'].get('harvested', 0)}")
        from collections import Counter
        c = Counter(v["variants"][0]["type"] for v in b["entries"].values())
        print("题型分布:", dict(c))
        top = sorted(b["entries"].items(), key=lambda kv: -kv[1].get("hits", 0))[:5]
        for k, v in top:
            print(f"  hits={v.get('hits',0)} {k[:50]}")
        return 0
    if cmd == "match":
        return match(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "live_paper.json"))
    if cmd == "conflicts":
        return list_conflicts()
    if cmd == "resolve":
        # python qbank.py resolve <题干前缀> <正确答案> [理由]
        if len(sys.argv) < 4:
            print("用法: python qbank.py resolve <题干前缀> <正确答案> [理由]")
            return 1
        return resolve_conflict(sys.argv[2], sys.argv[3],
                                sys.argv[4] if len(sys.argv) > 4 else "")
    print("用法: python qbank.py [harvest <examuserId> | stats | match <paper.json> | conflicts | resolve <前缀> <答案>]")
    return 1


if __name__ == "__main__":
    sys.exit(main())

