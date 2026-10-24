#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
textbook_verify.py — 教材答案反向验证引擎

不依赖题干定位(OCR 双栏拼接会切断题干), 而是:
  给定题干 + 一个候选答案, 判断该答案是否"填得进"教材原文。

三个独立证据:
  E1 答案本身在教材中出现次数(罕见词更强)
  E2 题干中"离空最近的 6 字片段"能否在教材中定位, 且该处上下文含候选答案
  E3 {input} 的后续文字(suffix)是否紧跟在候选答案之后出现

评分 = 加权组合, 输出 0~1 置信度。
"""
import re
import unicodedata
from difflib import SequenceMatcher

PAGE_MARK = re.compile(r"=+\s*PAGE\s*\d+\s*=+", re.I)
SENT = "\uE000"

_OCR_FIX = {
    "．": ".", "，": ",", "；": ";", "：": ":",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")", "《": "<", "》": ">",
    "％": "%", "　": " ",
}


def normalize(s: str) -> str:
    if not s:
        return ""
    s = PAGE_MARK.sub("", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"^\s*\+\s*[\d.]+", "", s)
    s = unicodedata.normalize("NFKC", s)
    for a, b in _OCR_FIX.items():
        s = s.replace(a, b)
    return re.sub(r"[\s\u3000]+", "", s)


def prep_stem(title: str):
    """题干 -> (prefix, suffix); placeholder 统一成哨兵。"""
    t = re.sub(r"[（(]\s*(?:专题[^）)]*|目录)\s*[）)]", "", title or "")
    t = re.sub(r"\{input\}", SENT, t)
    t = re.sub(r"[_＿]{2,}", SENT, t)
    t = re.sub(r"[（(]\s{2,}[）)]", SENT, t)
    t = normalize(t)
    if SENT not in t:
        return None, None
    i = t.index(SENT)
    return t[:i], t[i + 1:]


class Book:
    def __init__(self, path):
        self.text = normalize(open(path, encoding="utf-8").read())
        self._idx = None

    def occurrences(self, needle: str, cap: int = 400):
        """返回 needle 在教材中的所有起始位置。"""
        if not needle:
            return []
        out, start = [], 0
        while len(out) < cap:
            i = self.text.find(needle, start)
            if i < 0:
                break
            out.append(i)
            start = i + 1
        return out

    # ---------- 证据 ----------
    def e1_answer_present(self, cand: str) -> float:
        """候选答案在教材中是否出现。"""
        if not cand:
            return 0.0
        n = len(self.occurrences(cand, cap=50))
        if n == 0:
            return 0.0
        return 1.0

    def e2_anchor_context(self, prefix: str, cand: str, anchor_len: int = 6) -> float:
        """题干尾片段定位, 检查该位置附近是否紧接着出现候选答案。"""
        if not prefix or len(prefix) < anchor_len or not cand:
            return 0.0
        anchor = prefix[-anchor_len:]
        best = 0.0
        for pos in self.occurrences(anchor, cap=60):
            end = pos + len(anchor)
            window = self.text[end:end + len(cand) + 14]
            if not window:
                continue
            j = window.find(cand)
            if j >= 0:
                # 越靠近锚点越可信
                best = max(best, 1.0 if j <= 4 else 0.7)
            else:
                # 容错: 允许 OCR 单字差
                seg = window[:max(len(cand), 6)]
                r = SequenceMatcher(None, cand, seg).ratio()
                if r >= 0.75:
                    best = max(best, 0.5 * r)
        return best

    def e3_suffix_follows(self, suffix: str, cand: str, cand_len_search: int = 8) -> float:
        """候选答案之后是否紧跟 {input} 后面的文字。"""
        if not suffix or len(suffix) < 4 or not cand:
            return 0.0
        suf_head = suffix[:6]
        best = 0.0
        for pos in self.occurrences(cand, cap=80):
            after = self.text[pos + len(cand):pos + len(cand) + len(suf_head) + 10]
            if suf_head in after[:len(suf_head) + 6]:
                best = 1.0
                break
            r = SequenceMatcher(None, suf_head, after[:len(suf_head)]).ratio()
            if r >= 0.7:
                best = max(best, r)
        return best

    def score(self, prefix, suffix, cand, w=(0.25, 0.45, 0.30)):
        if not cand:
            return 0.0, {}
        e1 = self.e1_answer_present(cand)
        e2 = self.e2_anchor_context(prefix, cand)
        e3 = self.e3_suffix_follows(suffix, cand)
        total = w[0] * e1 + w[1] * e2 + w[2] * e3
        return total, {"e1": round(e1, 2), "e2": round(e2, 2), "e3": round(e3, 2)}
