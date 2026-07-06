# -*- coding: utf-8 -*-
"""
Query normalization helpers.

重點：
- 把各種怪空白（全形、NBSP、零寬等）正規化
- 在「英數 ↔ 中文」之間自動插入空白（S1012規格 → S1012 規格）
- 只移除「中文與中文之間」的多餘空白，不動英文單字間的空白
"""
import re

# 各種 Unicode 空白（含全形/零寬/窄不間斷等）
_U_SPACES = r"\u00A0\u1680\u180E\u2000-\u200A\u202F\u205F\u3000\u200B"

# 預編 regex
WS_WIDE_PAT       = re.compile(f"[{_U_SPACES}]+")
MULTI_WS_PAT      = re.compile(r"\s+")
CJK_JOIN_WS_PAT   = re.compile(r"([\u4E00-\u9FFF])\s+([\u4E00-\u9FFF])")
CJK_PUNCT_L_PAT   = re.compile(r"([\u4E00-\u9FFF])\s+([？?！!，,。．。；;：:、])")
CJK_PUNCT_R_PAT   = re.compile(r"([？?！!，,。．。；;：:、])\s+([\u4E00-\u9FFF])")
TRAILING_Q_PAT    = re.compile(r"[嗎吗呢嘛唄吧啦！？?!\s]+$")

# NEW: 英數 與 中文 之間強制加空白（解 S1012規格 / E1080規格 等無空格案例）
ALNUM_CJK_L_PAT   = re.compile(r"([A-Za-z0-9_.+\-/#])([\u4E00-\u9FFF])")
CJK_ALNUM_R_PAT   = re.compile(r"([\u4E00-\u9FFF])([A-Za-z0-9_.+\-/#])")

def normalize_whitespace(s: str) -> str:
    """
    將字串中的怪空白統一為一般空白，並處理中英混排、中文標點周邊空白。
    """
    if not s:
        return s

    # 1) 怪空白 → 一般空白
    s = WS_WIDE_PAT.sub(" ", s)

    # 2) NEW: 英數 ↔ 中文 之間插入空白（S1012規格 → S1012 規格）
    s = ALNUM_CJK_L_PAT.sub(r"\1 \2", s)
    s = CJK_ALNUM_R_PAT.sub(r"\1 \2", s)

    # 3) 連續空白壓成一個
    s = MULTI_WS_PAT.sub(" ", s)

    # 4) 中文與中文之間多餘空白移除
    s = CJK_JOIN_WS_PAT.sub(r"\1\2", s)

    # 5) 中文與標點的多餘空白移除
    s = CJK_PUNCT_L_PAT.sub(r"\1\2", s)
    s = CJK_PUNCT_R_PAT.sub(r"\1\2", s)

    return s.strip()

def normalize_for_bm25(s: str) -> str:
    """分詞/關鍵字檢索前的安全清理。"""
    return normalize_whitespace(s)

__all__ = ["normalize_whitespace", "normalize_for_bm25"]
