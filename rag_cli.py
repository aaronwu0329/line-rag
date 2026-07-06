import os
import re
import math
import faiss
import time
import numpy as np
import pyarrow.parquet as pq
from sentence_transformers import SentenceTransformer, CrossEncoder
from linebot.v3.messaging import FlexMessage
from dotenv import load_dotenv
from product_faq import _is_official_site_intent, try_company_or_brand_list
from flask import Flask, request, abort
from werkzeug.middleware.proxy_fix import ProxyFix  # ★ 反向代理修正
import random
import logging
import requests  # ★ 送圖前 HEAD 健檢
from urllib.parse import urljoin
from fallback import enable_fallback

from product_faq import try_company_or_brand_list, autocomplete_weak_query
from query_normalize import normalize_whitespace, normalize_for_bm25

# watsonx.ai SDK
from ibm_watsonx_ai import APIClient
from ibm_watsonx_ai.foundation_models import ModelInference

# LINE v3 SDK
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import ApiException
from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration,
    ReplyMessageRequest, TextMessage,
    TemplateMessage, ButtonsTemplate, URIAction,
    StickerMessage, ImageMessage,
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, StickerMessageContent
)

# ---- 文字檢索（BM25） ----
import jieba
from rank_bm25 import BM25Okapi
from typing import Optional, Tuple  # ★ 顧問流程用

# =========================================================
# 0) 讀環境變數 + 日誌
# =========================================================
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

IBM_API_KEY    = os.getenv("IBM_API_KEY")
IBM_PROJECT_ID = os.getenv("IBM_PROJECT_ID")
IBM_CLOUD_URL  = os.getenv("IBM_CLOUD_URL", "https://us-south.ml.cloud.ibm.com")
IBM_MODEL_ID   = os.getenv("IBM_MODEL_ID", "openai/gpt-oss-120b")

# Reranker
RERANKER_ID_PRIMARY   = os.getenv("RERANKER_ID", "BAAI/bge-reranker-v2-m3")
RERANKER_ID_FALLBACK  = os.getenv("RERANKER_FALLBACK_ID", "cross-encoder/ms-marco-MiniLM-L-6-v2")

# Image bases（重要：請放「公開 HTTPS」）
BRAND_ASSET_BASE_URL   = os.getenv("BRAND_ASSET_BASE_URL", "").strip()       # 原圖/一般圖
BRAND_PREVIEW_BASE_URL = os.getenv("BRAND_PREVIEW_BASE_URL", "").strip()     # 預覽縮圖（建議長效公開）
PLACEHOLDER_IMAGE_URL  = os.getenv("PLACEHOLDER_IMAGE_URL", "").strip()      # 取不到品牌圖時的保底

if not (IBM_API_KEY and IBM_PROJECT_ID):
    raise RuntimeError("請在 .env 設定 IBM_API_KEY 與 IBM_PROJECT_ID（必要）。")
if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET):
    raise RuntimeError("請在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET。")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='[%(levelname)s] %(asctime)s %(message)s'
)

logging.info(f"[IMAGE] BRAND_ASSET_BASE_URL={BRAND_ASSET_BASE_URL!r}")
logging.info(f"[IMAGE] BRAND_PREVIEW_BASE_URL={BRAND_PREVIEW_BASE_URL!r}")
logging.info(f"[IMAGE] PLACEHOLDER_IMAGE_URL={PLACEHOLDER_IMAGE_URL!r}")

def _short(s: str, n: int = 140) -> str:
    return (s or "").replace("\n", " ")[:n]

def _log_top5_after_rerank(query: str, top_rows, reranker_name: str):
    logging.info(f'[RERANK] query="{query}" | reranker="{reranker_name}" | top={len(top_rows)}')
    for i, r in enumerate(top_rows, start=1):
        score = r.get("rerank_score", r.get("score", 0.0))
        title_short = (r.get("title") or "")[:80]
        text_short  = _short(r.get("text"), 160)
        logging.info(
            f'  {i}. score={score:.6f} | {r["vendor"]}/{r["doc_name"]}#{r["chunk_id"]} | '
            f'title="{title_short}" | text="{text_short}"'
        )

# =========================================================
# 1) watsonx.ai 初始化（穩定輸出 + 嚴格停點）
# =========================================================
creds = {"url": IBM_CLOUD_URL, "apikey": IBM_API_KEY}
wml_client = APIClient(creds)
wml_client.set.default_project(IBM_PROJECT_ID)

MAX_NEW_TOKENS       = int(os.getenv("MAX_NEW_TOKENS", "120"))
MAX_OUTPUT_CHARS     = int(os.getenv("MAX_OUTPUT_CHARS", "300"))
MAX_OUTPUT_SENTENCES = int(os.getenv("MAX_OUTPUT_SENTENCES", "3"))

GEN_PARAMS = {
    "decoding_method": "greedy",
    "max_new_tokens": MAX_NEW_TOKENS,
    "temperature": 0,
    "repetition_penalty": 1.15,
    "stop_sequences": [
        "<|endoftext|>",
        "<|eot_id|>",
        "Answer:",
        "Step-by-step",
        "Output",
        "你的回應是：",
    ],
}

wx_model = ModelInference(
    model_id=IBM_MODEL_ID,
    params=GEN_PARAMS,
    credentials=creds,
    project_id=IBM_PROJECT_ID,
)

# =========================================================
# 2) RAG 索引/嵌入 + Hybrid Retrieval + Reranker
# =========================================================
INDEX_PATH     = "rag_index/md_chunks.faiss"
META_PATH      = "rag_index/md_meta.parquet"
EMBED_MODEL    = "intfloat/multilingual-e5-base"
INITIAL_K      = 20
RERANK_TOP_K   = 5
VEC_K_EACH     = 20
KW_K_EACH      = 20
RRF_K          = 60.0
ALPHA_VEC      = 0.5
RELEVANCE_TH_MODEL = 0.18
RELEVANCE_TH   = 0.32
MAX_CTX_CHARS  = 2200

print("Loading FAISS index & meta parquet ...")
index = faiss.read_index(INDEX_PATH)
meta  = pq.read_table(META_PATH).to_pandas()

texts     = meta["text"].fillna("").astype(str).tolist()
titles    = meta["title"].fillna("").astype(str).tolist()
vendors   = meta["vendor"].fillna("").astype(str).tolist()
doc_names = meta["doc_name"].fillna("").astype(str).tolist()
chunk_ids = meta["chunk_id"].tolist()

embedder = SentenceTransformer(EMBED_MODEL)

# ---- BM25 ----
def _tok_zh_en(s: str):
    s = normalize_for_bm25(s or "")
    zh_tokens = [t for t in jieba.lcut(s) if t.strip()]
    en = re.findall(r"[A-Za-z0-9_.+\-/#]+", s)
    en_std = en + [e.lower() for e in en if any(c.isalpha() for c in e)]
    return zh_tokens + en_std

bm25_corpus_tokens = [_tok_zh_en(normalize_for_bm25(t)) for t in texts]
bm25 = BM25Okapi(bm25_corpus_tokens)

# ---- Cross-Encoder Reranker ----
try:
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
except Exception:
    device = "cpu"

def _get_ce_name(ce) -> str:
    name = getattr(ce, "model_name", None)
    if name:
        return name
    cfg = getattr(getattr(ce, "model", None), "config", None)
    if cfg:
        name = getattr(cfg, "_name_or_path", None) or getattr(cfg, "name_or_path", None)
        if name:
            return name
    tok = getattr(ce, "tokenizer", None)
    if tok:
        name = getattr(tok, "name_or_path", None)
        if name:
            return name
    return ce.__class__.__name__

HAS_RERANKER = False
reranker = None
RERANKER_NAME = "N/A"
RERANKER_DEVICE = device

try:
    reranker = CrossEncoder(RERANKER_ID_PRIMARY, device=device)
    RERANKER_NAME = _get_ce_name(reranker)
    HAS_RERANKER = True
    logging.info(f"[INIT] Reranker primary loaded: {RERANKER_NAME} (device={device})")
    if RERANKER_ID_PRIMARY.lower() not in RERANKER_NAME.lower():
        logging.warning(f"[INIT] Requested '{RERANKER_ID_PRIMARY}' but loaded '{RERANKER_NAME}'")
except Exception as e1:
    logging.warning(f"[RERANKER] primary '{RERANKER_ID_PRIMARY}' failed: {e1}. Trying fallback...")
    try:
        reranker = CrossEncoder(RERANKER_ID_FALLBACK, device=device)
        RERANKER_NAME = _get_ce_name(reranker)
        HAS_RERANKER = True
        logging.info(f"[INIT] Reranker fallback loaded: {RERANKER_NAME} (device={device})")
    except Exception as e2:
        reranker = None
        HAS_RERANKER = False
        RERANKER_NAME = "N/A (use fused score only)"
        logging.error(f"[RERANKER] fallback '{RERANKER_ID_FALLBACK}' failed: {e2}. Using fused score only.")

# =========================================================
# 檢索工具
# =========================================================
def _vector_search(query: str, k: int):
    q_norm = normalize_whitespace(query or "")
    q_for_embed = f"query: {q_norm}" if q_norm else ""
    q = embedder.encode([q_for_embed], convert_to_numpy=True, normalize_embeddings=True)
    D, I = index.search(q, k)
    dists = D[0]; idxs = I[0]
    return [(int(i), float(d)) for i, d in zip(idxs.tolist(), dists) if int(i) >= 0]

_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-/#]*")

def extract_en_terms(q: str) -> list[str]:
    terms = _EN_RE.findall(q or "")
    return [t for t in terms if len(t) >= 2]


def _keyword_search(query: str, k: int, q_en: str = None):
    query_n = normalize_for_bm25(query or "")
    tokens = _tok_zh_en(query_n)
    if q_en and q_en != query:
        tokens += _tok_zh_en(normalize_for_bm25(q_en))

    scores = np.asarray(bm25.get_scores(tokens), dtype=float)
    n_docs = scores.size
    if n_docs == 0:
        return []
    max_score = float(scores.max())
    if max_score <= 0:
        return []
    n = min(k, n_docs)
    top_idx = np.argpartition(scores, -n)[-n:]
    top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]
    sims = (scores[top_idx] / max_score).tolist()
    return list(zip(top_idx.tolist(), sims))


def _rrf_fuse(vec_pairs, kw_pairs, out_k: int):
    v_sorted = sorted(vec_pairs, key=lambda x: x[1], reverse=True)
    k_sorted = sorted(kw_pairs,  key=lambda x: x[1], reverse=True)
    rrf = {}
    for rank, (i, _) in enumerate(v_sorted, start=1):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (i, _) in enumerate(k_sorted, start=1):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank)
    fused = {}
    v_map = {i: s for i, s in vec_pairs}
    k_map = {i: s for i, s in kw_pairs}
    all_ids = set(v_map.keys()) | set(k_map.keys())
    for i in all_ids:
        fused[i] = rrf.get(i, 0.0) + (ALPHA_VEC * v_map.get(i, 0.0)) + ((1 - ALPHA_VEC) * k_map.get(i, 0.0))
    top = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:out_k]
    results = []
    for idx, score in top:
        if 0 <= idx < len(texts):
            results.append({
                "idx": idx,
                "score": float(score),
                "text": texts[idx],
                "title": titles[idx],
                "vendor": vendors[idx],
                "doc_name": doc_names[idx],
                "chunk_id": chunk_ids[idx],
            })
    return results

# ★ 在 import 區加：
from opencc import OpenCC

# ★ 在 watsonx 初始化區塊附近加：
CC_TW = OpenCC('s2twp')  # 簡→繁（臺灣詞彙）
def to_trad_tw(s: str) -> str:
    try:
        return CC_TW.convert(s or "").strip()
    except Exception:
        return (s or "").strip()


# =========================================================
# 中文→英文 翻譯器 (只用於檢索)
# =========================================================
def call_llm(prompt: str) -> str:
    try:
        system_preamble = (
            "你是企業文件助理。只輸出一段精簡的繁體中文結論。"
            "禁止出現任何開場白、前言、導語或教學語氣，例如："
            "「讓我來說明」、「以下是」、「我將給你」、「這裡是」、「我會幫你」、「範例」、「回答如下」等字樣。"
            "嚴禁出現條列、步驟、分析、原答案、參考答案、Answer:、Output、References 等字樣。"
            "若不慎產生上述內容，請刪除前文，只保留最終結論。\n\n"
        )
        full_prompt = system_preamble + prompt
        resp = wx_model.generate(prompt=full_prompt)
        ans = _extract_text_from_wx(resp)
        return to_trad_tw(_finalize(ans)) 
    except Exception as e:
        return f"(LLM error: {e})"

# ★ 新增：原生生成（不加 system、不做 finalize），專供翻譯使用
def raw_generate(prompt: str) -> str:
    try:
        resp = wx_model.generate(prompt=prompt)
        return _extract_text_from_wx(resp).strip()
    except Exception as e:
        return f"(LLM error: {e})"

def translate_zh_to_en(q_zh: str) -> str:
    prompt = (
        "Translate the following Chinese query into concise English keywords or a short phrase.\n"
        "Only output English. No explanations, no extra punctuation.\n"
        f"{q_zh}"
    )
    ans = raw_generate(prompt).strip()
    if ans.startswith("(LLM error:"):
        return q_zh
    return ans if re.search(r"[A-Za-z]", ans) and not re.search(r"[\u4e00-\u9fff]", ans) else q_zh

def hybrid_retrieve(query: str, out_k: int = INITIAL_K):
    q_pre = normalize_whitespace(query or "")
    vec_pairs_zh = _vector_search(q_pre, VEC_K_EACH)
    q_for_vec_en = translate_zh_to_en(q_pre)
    vec_pairs_en = []
    if q_for_vec_en and q_for_vec_en != q_pre:
        vec_pairs_en = _vector_search(q_for_vec_en, VEC_K_EACH)
    en_terms = extract_en_terms(q_pre)
    vec_pairs_terms = []
    for t in en_terms:
        if t and len(t) >= 2:
            vec_pairs_terms.extend(_vector_search(t, max(1, VEC_K_EACH // 2)))
    kw_pairs = _keyword_search(q_pre, KW_K_EACH, q_en=q_for_vec_en)
    all_vec_pairs = vec_pairs_zh + vec_pairs_en + vec_pairs_terms
    return _rrf_fuse(all_vec_pairs, kw_pairs, out_k)

# =========================================================
# 片段清理 + 組上下文 + LLM 後處理
# =========================================================
_HDR_RE = re.compile(r'^\s*(#{1,6}|\*|-|\d+[\.\)]|（?\d+）?)\s+')
_BAD_HEAD_RE = re.compile(
    r'^(?:'
    r'(?:Step|步驟|Output|分析|解析|原答案|參考答案|Answer|Solution|Reference|References|來源)'
    r'|(?:你的回應是|以下(?:是|為)|回覆內容|解答|回答)\s*[:：]'
    r'|如果你的回應'
    r')\b',
    re.I
)
_NAV_TRASH = re.compile(
    r'^(?:問題|問答|Q[:：]?|前往首頁|返回|查看更多|更多資訊|什麼是|立即開始|點此|目錄)\b',
    re.I
)

def _sanitize_ctx_block(s: str) -> str:
    keep = []
    for line in (s or "").splitlines():
        ln = line.strip()
        if not ln:
            continue
        if ln.startswith(("http://", "https://", "//")):
            continue
        if _HDR_RE.match(ln):
            continue
        if _BAD_HEAD_RE.match(ln) or _NAV_TRASH.match(ln):
            continue
        keep.append(ln)
    return " ".join(keep)

def build_context(top_rows, max_chars: int = MAX_CTX_CHARS):
    ctx_parts, used, curr = [], [], 0
    for r in top_rows:
        piece = _sanitize_ctx_block(r['text'] or "")
        if not piece:
            continue
        piece = piece.strip() + "\n\n"
        if curr + len(piece) > max_chars:
            break
        ctx_parts.append(piece); used.append(r["idx"]); curr += len(piece)
    return "".join(ctx_parts), used

def _extract_text_from_wx(resp) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        if "results" in resp and resp["results"]:
            item = resp["results"][0]
            if isinstance(item, dict) and "generated_text" in item:
                return item["generated_text"]
        return str(resp)
    if isinstance(resp, list):
        first = resp[0] if resp else {}
        if isinstance(first, dict) and "generated_text" in first:
            return first["generated_text"]
        return str(resp)
    return str(resp)

def _strip_markdown(s: str) -> str:
    s = re.sub(r"^#+\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"`{1,3}.*?`{1,3}", "", s, flags=re.DOTALL)
    s = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", s)
    s = re.sub(r"^\s*[-*]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", s)
    return s.strip()

def _dedupe_text(ans: str) -> str:
    ans = re.sub(r"[ \t]+", " ", ans)
    ans = re.sub(r"\n{3,}", "\n\n", ans).strip()
    paras = [p.strip() for p in re.split(r"(?:\r?\n){2,}", ans) if p.strip()]
    seen, kept = set(), []
    for p in paras:
        if p not in seen:
            seen.add(p); kept.append(p)
    ans = "\n\n".join(kept)
    ans = re.sub(r"^(最終答案[：:]\s*)", "", ans)
    ans = re.sub(r"\[\s*\d+\s*\]", "", ans)
    return ans.strip()

def _strip_special_tokens(s: str) -> str:
    return re.sub(
        r'(?:<\|endoftext\|>|<\|end_of_text\|>|<\|eot_id\|>|<\|eot\|>|'
        r'<\|assistant\|>|<\|user\|>|<\|system\|>|</s>|<s>)',
        '',
        s
    )

def _strip_chunk_refs(s: str) -> str:
    s = re.sub(r'\b[Cc]hunk\s*\d+\b', '', s)
    s = re.sub(r'[（(]?[Cc]hunk\s*\d+[)）]?', '', s)
    s = re.sub(r'(?:片段|段落)\s*\d+\s*', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s

def _strip_hash_noise(s: str) -> str:
    if not s:
        return s
    s = re.sub(r'^\s*#{1,6}\s*', '', s)
    s = re.sub(r'(?<=\s)#{1,6}(?=\s|$)', ' ', s)
    s = re.sub(r'\s*#{1,6}\s*$', '', s)
    s = re.sub(r'([。！？!?])\s*#{1,6}\s*$', r'\1', s)
    return re.sub(r'\s{2,}', ' ', s).strip()

def _enforce_one_paragraph(s: str) -> str:
    m = re.search(r'(?:^|[\r\n])\s*(?:答案|答覆|Answer)\s*[:：]\s*(.*)', s, flags=re.IGNORECASE | re.DOTALL)
    if m:
        s = m.group(1).strip()
    clean_lines = []
    for line in s.splitlines():
        if re.match(r'^\s*(?:Step|步驟)\s*\d+\s*[:：]?', line, flags=re.IGNORECASE):
            continue
        if re.match(r'^\s*(?:\d+[\.\)]|[（(]?\d+[)）]|[•\-–—*])\s+', line):
            continue
        clean_lines.append(line.strip())
    s = " ".join([ln for ln in clean_lines if ln])
    s = re.sub(r'\s+', ' ', s).strip()
    return s

_CUT_MARK = re.compile(
    r'(?:原答案|參考答案|答案[:：]|Answer:|Step\-by\-step|Output|References?|'
    r'如果你的回應|你的回應是[:：]|以下(?:是|為)[:：])',
    re.I
)
_CJK_SENT_SPLIT = re.compile(r'(?<=[。！？!?])')

def _limit_sentences(s: str, n: int) -> str:
    parts = [p.strip() for p in _CJK_SENT_SPLIT.split(s) if p.strip()]
    return " ".join(parts[:max(1, n)])
    
# 把尾端的標點/雜訊整串，統一換成一個「。」；若本來就是「。」或省略號就不動
_TAIL_TO_PERIOD = re.compile(r'[\s\.\!！\?？；;、，,~～…⋯‥:：\)\]\}」』》>】]+$')

def force_zh_period(s: str) -> str:
    s = (s or "").rstrip()
    if not s:
        return s
    # 保留有意圖的收尾：全形句號或省略號
    if s.endswith('。') or re.search(r'(…|\.{3})$', s):
        return s
    # 把尾巴不是句號的東西整串去掉，再補一個「。」
    s = _TAIL_TO_PERIOD.sub('', s)
    return s + '。'


def _finalize(ans: str) -> str:
    ans = _strip_markdown(ans)
    ans = _dedupe_text(ans)
    ans = _strip_special_tokens(ans)
    ans = _strip_chunk_refs(ans)
    m = _CUT_MARK.search(ans)
    if m:
        ans = ans[:m.start()]
    ans = _enforce_one_paragraph(ans)
    ans = _strip_hash_noise(ans)
    ans = _limit_sentences(ans, MAX_OUTPUT_SENTENCES)

    if len(ans) > MAX_OUTPUT_CHARS:
        cut = ans[:MAX_OUTPUT_CHARS]
        # 優先在最近的句尾符收斷
        m = re.search(r'(?s).*?[。！？!?；;](?=[^。！？!?；;]*$)', cut)
        ans = (m.group(0) if m else cut.rstrip("，、；：:,. \n"))

    # ★ 統一：把截斷後的尾巴強制改成「。」（若已是「。」或省略號則不動）
    ans = force_zh_period(ans)
    return ans.strip()



def rerank_with_crossencoder(query: str, candidates, top_k: int = RERANK_TOP_K):
    if not HAS_RERANKER or not candidates:
        return sorted(candidates, key=lambda d: (-d["score"], d["idx"]))[:top_k]
    pairs = [(query, c["text"]) for c in candidates]
    scores = reranker.predict(pairs)
    for i, s in enumerate(scores):
        candidates[i]["rerank_score"] = float(s)
    ranked = sorted(
        candidates,
        key=lambda d: (-(d.get("rerank_score", d["score"])), d["idx"])
    )
    return ranked[:top_k]

# =========================================================
# === 新增小工具（只加、不改動其他架構）
# =========================================================
_ZH_FILLERS = {
    "介紹一下", "請介紹", "幫我介紹", "麻煩介紹", "介紹下",
    "請問", "幫我", "一下", "一下下", "如何", "怎麼", "麻煩", "說明", "是什麼", "有哪些",
    "介紹"
}

def canonicalize_query(q: str) -> str:
    q = _to_ascii(normalize_whitespace(q or ""))
    q = re.sub(r"[，。！？、；：,.!?;:]+", " ", q)
    for w in sorted(_ZH_FILLERS, key=len, reverse=True):
        q = q.replace(w, " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q

_SPEC_SYNS = ["規格", "規格表", "规格", "spec", "specs", "specifications", "datasheet", "technical details"]
def expand_spec_synonyms(q: str, hit_models: list[str]) -> str:
    need = bool(hit_models) or re.search(r"(規格|规格|spec|datasheet|technical\s+details)", q, re.I)
    if not need:
        return q
    extra = " ".join(_SPEC_SYNS + ["CPU", "memory", "I/O", "slots", "power"])
    return f"{q} {extra}".strip()

def _as_question_for_prompt(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return q
    if re.search(r'[。.!！?？]$', q):
        return q
    return q + "？"

import re

_GIB_VALID_WORD = re.compile(
    r"(?:"                                             
    r"[\u4e00-\u9fff]{2,}"                              
    r"|[A-Za-z]{2,}"                                    
    r"|[A-Za-z]\d{2,}[A-Za-z0-9]*"                      
    r"|\d{3,}"                                          
    r"|[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+"             
    r")"
)
_GIB_SINGLE_ALNUM = re.compile(r"^[A-Za-z0-9]{2,8}$")
_GIB_NOISE_RUN = re.compile(r"[^\w\s\u4e00-\u9fff]{3,}")
_GIB_REPEAT_CHAR = re.compile(r"(.)\1{4,}")
_GIB_VOWELLESS = re.compile(r"\b[b-df-hj-np-tv-z]{6,}\b", re.IGNORECASE)
_ALLOWED_PUNCTS = set(".,!?，。！？：；、-–—_/+&()[]{}#@'\"%*<>=~")

if "MODEL_PAT" not in globals():
    MODEL_PAT = re.compile(r"[A-Za-z]\d{2,}[A-Za-z0-9]*")

def _symbol_ratio(text: str) -> float:
    if not text:
        return 1.0
    total = len(text)
    sym = 0
    for ch in text:
        if ch.isalnum() or '\u4e00' <= ch <= '\u9fff' or ch.isspace():
            continue
        if ch not in _ALLOWED_PUNCTS:
            sym += 1
    return sym / max(total, 1)

def _is_ime_mistype(s: str) -> bool:
    ascii_part = re.sub(r"[\u4e00-\u9fff]", " ", s)
    if not re.search(r"[ /\\\-\._,;:]", ascii_part):
        return False
    frags = [f for f in re.split(r"[ \t,;:/\\\-\._]+", ascii_part) if f]
    if not frags:
        return False
    short = sum(1 for f in frags if len(f) <= 2 and re.fullmatch(r"[A-Za-z0-9]+", f))
    long_ = sum(1 for f in frags if len(f) >= 3)
    joined = "".join(frags)
    alpha_ratio = (sum(c.isalpha() for c in joined) / len(joined)) if joined else 0.0
    if long_ == 0 and alpha_ratio >= 0.5:
        if short >= 2:
            return True
        if short >= 1 and len(joined) <= 4:
            return True
    return False

CLARIFY_MSG = (
    "看起來像是縮寫或無法辨識的輸入（可能輸入法未切換或符號過多）。\n"
    "請提供更完整的描述，例如：\n"
    "• IBM Power E1080 規格\n"
    "• MongoDB Enterprise 是什麼？\n"
    "• 介紹 IBM watsonx.ai\n"
    "也可以提供品牌＋型號或關鍵字（如：IBM S1022s、Power10 記憶體上限）。"
)

def looks_like_gibberish(q_raw: str, hit_models: list[str], hit_brands: list[str]) -> bool:
    s = _to_ascii(normalize_whitespace(q_raw or ""))
    # ★ 新增：只要偵測到品牌或型號，直接視為有效，避免被當成雜訊
    if hit_models or hit_brands:
        return False

    if not s:
        return True

    if re.search(r"[\u4e00-\u9fff]", s):
        if _is_ime_mistype(s):
            return True
        if not _GIB_VALID_WORD.search(s) or _symbol_ratio(s) > 0.35:
            return True
        if _GIB_NOISE_RUN.search(s) and _symbol_ratio(s) > 0.30:
            return True
        return False

    s2 = re.sub(r"[，。！？、；：,.!?;:()\[\]{}<>/\\\-_=+~`'\"|@#$%^&*]", " ", s)
    s2 = re.sub(r"\s+", " ", s2).strip()
    if not s2:
        return True
    if _GIB_REPEAT_CHAR.search(s2):
        return True
    if _GIB_NOISE_RUN.search(s):
        if not _GIB_VALID_WORD.search(s2) or _symbol_ratio(s) > 0.35:
            return True
    sym_ratio = _symbol_ratio(s)
    tokens = _GIB_VALID_WORD.findall(s2)
    token_cnt = len(tokens)
    toks = s2.split()
    if len(toks) == 1:
        t = toks[0]
        if MODEL_PAT.match(t):
            return False
        if _GIB_SINGLE_ALNUM.fullmatch(t):
            return True
    if token_cnt == 0 and len(s2.replace(" ", "")) <= 10:
        return True
    if _GIB_VOWELLESS.search(s2) and sym_ratio > 0.25:
        return True
    if token_cnt == 1 and sym_ratio > 0.30:
        return True
    return False


def row_contains_any_model(row: dict, models: list[str]) -> bool:
    if not row or not models:
        return False
    pat = re.compile("|".join(map(re.escape, models)), re.I)
    return bool(pat.search(row.get("title","")) or pat.search(row.get("text","")))


# =========================================================
def normalize_brand_case(q: str) -> str:
    rules = [
        (r'(?<![A-Za-z0-9])mongo\s*db(?![A-Za-z0-9])',        'MongoDB'),
        (r'(?<![A-Za-z0-9])mongodb(?![A-Za-z0-9])',            'MongoDB'),
        (r'(?<![A-Za-z0-9])ibm(?![A-Za-z0-9])',                'IBM'),
        (r'(?<![A-Za-z0-9])palo[\s\-]*alto(?![A-Za-z0-9])',    'Palo Alto'),
        (r'(?<![A-Za-z0-9])qlik(?![A-Za-z0-9])',               'Qlik'),
        (r'(?<![A-Za-z0-9])splunk(?![A-Za-z0-9])',             'Splunk'),
        (r'(?<![A-Za-z0-9])suse(?![A-Za-z0-9])',               'SUSE'),
        (r'(?<![A-Za-z0-9])synopsys(?![A-Za-z0-9])',           'Synopsys'),
        (r'(?<![A-Za-z0-9])tibco(?![A-Za-z0-9])',              'TIBCO'),
        (r'(?<![A-Za-z0-9])cloudera(?![A-Za-z0-9])',           'Cloudera'),
        (r'(?<![A-Za-z0-9])cloud\s*casa(?![A-Za-z0-9])',       'CloudCasa'),
        (r'(?<![A-Za-z0-9])instana(?![A-Za-z0-9])',            'Instana'),
        (r'(?<![A-Za-z0-9])watsonx(?:\.ai)?(?![A-Za-z0-9])',   'watsonx.ai'),
        (r'(?<![A-Za-z0-9])watsonx\.data(?![A-Za-z0-9])',      'watsonx.data'),
        (r'(?<![A-Za-z0-9])watsonx\.governance(?![A-Za-z0-9])','watsonx.governance'),
    ]
    for pat, repl in rules:
        q = re.sub(pat, repl, q, flags=re.IGNORECASE)
    return q

_META_PREAMBLE_RE = re.compile(
    r'^\s*(?:這裡是|以下(?:是|為)?|下面是|給你|提供你|範例|示例|樣例|提示|建議|說明)\s*'
    r'(?:範例|示例|樣例)?(?:答案|回覆|回應|內容|敘述|解釋|結論)?\s*[:：]\s*',
    re.I
)
_SPEAKY_LEADS_RE = re.compile(
    r'^\s*(?:你可以|可嘗試|建議你|我(?:們)?可以|讓我們|接著我們|現在我來)\S*[:：]?\s*',
    re.I
)
def _strip_meta_preamble(s: str) -> str:
    s2 = _META_PREAMBLE_RE.sub('', s)
    s2 = _SPEAKY_LEADS_RE.sub('', s2)
    return s2.strip()

#（此處的 _finalize 已在上方覆蓋）

def rag_answer(user_query: str) -> str:
    pre = try_company_or_brand_list(user_query)
    if pre:
        return pre

    q_raw = normalize_whitespace(user_query)
    hit_models = detect_models(q_raw)
    hit_brands = detect_brands(q_raw)

    if looks_like_gibberish(q_raw, hit_models, hit_brands):
        return CLARIFY_MSG

    q_norm = canonicalize_query(q_raw)
    q_norm = normalize_brand_case(q_norm)
    boosted_query = autocomplete_weak_query(q_norm)
    # ★ 改良重點：品牌補強判斷更寬鬆（含空格/去空格都比）
    BRAND_KEYS = {
        "mongodb", "ibm", "qlik", "splunk", "suse",
        "synopsys", "tibco", "cloudera",
        "palo alto", "paloalto",
        "instana", "cloud casa", "cloudcasa",
        "watsonx", "watsonx.ai"
    }

    q_lower = q_norm.lower()
    q_flat  = q_lower.replace(" ", "")  # 去空格版本
    has_brand_word = any(
        (b in q_lower) or (b.replace(" ", "") in q_flat)
        for b in BRAND_KEYS
    )
    if q_lower in BRAND_KEYS or has_brand_word or hit_brands:
        boosted_query = f"{q_norm} overview features"

    if hit_models:
        boosted_query = f"IBM Power {hit_models[0]} {boosted_query}".strip()

    boosted_query = expand_spec_synonyms(boosted_query, hit_models)

    candidates = hybrid_retrieve(boosted_query, out_k=INITIAL_K)
    top_rows = rerank_with_crossencoder(q_norm, candidates, top_k=RERANK_TOP_K)
    _log_top5_after_rerank(q_norm, top_rows, RERANKER_NAME)

    best = top_rows[0] if top_rows else None
    def _gating_score(d): return float(d.get("rerank_score", d.get("score", 0.0))) if d else 0.0
    any_has_model = any(row_contains_any_model(r, hit_models) for r in top_rows)
    gating_score = _gating_score(best)

    if best and (any_has_model or gating_score >= RELEVANCE_TH):
        ctx, _ = build_context(top_rows, max_chars=MAX_CTX_CHARS)
        prompt = (
            "以下是可用的背景片段文本（不要在回答中提到片段或 Chunk）：\n"
            f"{ctx}\n"
            "請根據以上內容，用一段精簡的繁體中文回答下列問題：\n"
            f"{_as_question_for_prompt(user_query)}\n"
        )
        ans = call_llm(prompt)
    else:
        hint = "（你也可以問：IBM 有哪些產品？、Palo Alto 有哪些解決方案？、SUSE 提供什麼？）"
        prompt = (
            "知識庫裡沒有找到足夠相關的內容。"
            "請你依照自身的一般知識與推理能力，用繁體中文盡量回答下列問題：\n"
            f"{_as_question_for_prompt(user_query)}\n"
        )
        ans = call_llm(prompt)
        ans = "（以下為一般知識推測，非知識庫內容）\n" + ans + "\n" + hint

    if len(ans) > 1900:
       ans = ans[:1900]  # 若你想保留「…」，可改成 ans[:1900] + "…"
    ans = force_zh_period(ans)
    return ans


# === 商用功能包 A：選型顧問（售前導流）
SELECTOR_PAT = re.compile(
    r"(?:怎麼選|推薦|適合用|用哪個|哪套|哪一套|哪個產品|選型|架構建議|方案建議|PoC|MVP|"
    r"怎麼結合|可以怎麼結合|如何結合|整合|整合建議|搭配|搭配建議|組合|怎麼搭配|建議|怎麼用|推薦搭配|串接建議|串接|結合)"
    r"|(?:recommend|choose|which\s+(?:product|solution)|architecture\s+advice|"
    r"(?:combine|integrate|integration|mix|how\s+to\s+combine|how\s+to\s+use|suggest|advice))",
    re.I
)

def extract_selector_slots(q: str) -> dict:
    ql = q.lower()
    slots = {
        "budget": re.search(r"(?:預算|budget)\s*[:：]?\s*(\d[\d,\.]*\s*(?:k|萬|千|m)?)", q),
        "scale":  re.search(r"(?:用戶|節點|事件|EPS|QPS|資料量)\s*[:：]?\s*([0-9\.]+[kKmM]?)", q),
        "deadline": re.search(r"(?:時程|deadline|時限|月份|月內|週內|天內)", ql),
        "cloud": re.search(r"(on[-\s]?prem|aws|azure|gcp|ibm\s*cloud|私有雲|公有雲)", ql),
        "must": re.findall(r"(?:必須|需要|必備|must)\s*[:：]?\s*([^\n，。]{2,20})", q),
        "nice": re.findall(r"(?:加分|希望|最好|nice\s*to\s*have)\s*[:：]?\s*([^\n，。]{2,20})", q),
    }
    return {
        "budget": slots["budget"].group(1) if slots["budget"] else "",
        "scale": slots["scale"].group(1) if slots["scale"] else "",
        "deadline": "有明確時程" if slots["deadline"] else "",
        "cloud": slots["cloud"].group(0) if slots["cloud"] else "",
        "must_list": ", ".join(slots["must"]) if slots["must"] else "",
        "nice_list": ", ".join(slots["nice"]) if slots["nice"] else "",
    }

def advisor_answer(user_query: str, kb_rows: list[dict]) -> str:
    # 將檢索到的前幾段內容整理成 context
    ctx = "\n".join([f"- {r['title']}: {r['text'][:260]}..." for r in kb_rows[:6]])
    slots = extract_selector_slots(user_query)

    # ==== 指令模板（System Prompt）====
    sys = (
        "你是 B2B 產品選型顧問。"
        "請以精簡的繁體中文回答，不超過一段話。"
        "回答結構可包含：『建議組合』、『為何適合』、『風險與限制』、『下一步』，"
        "但若內容過長，請自行濃縮重點，避免條列與冗長說明。"
        "僅引用給定的上下文資訊，不要編造內容。"
    )

    # ==== 實際提示內容 ====
    prompt = (
        f"{sys}\n\n"
        f"使用者需求：{user_query}\n"
        f"抽取參數：{slots}\n"
        f"參考知識：\n{ctx}\n"
        "請根據以上資訊生成一段不超過一段話的繁體中文回答。"
    )

    return call_llm(prompt)

# ---- 卡片安全工具 ---------------------------------------
def line_clean_text(s: str, max_len: int) -> str:
    """壓成單行並截長，避免 ButtonsTemplate 欄位違規。"""
    s = (s or "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len - 1] + "…"
    return s

def prepare_line_text(ans: str, limit: int = 480, keep_newlines: bool = True) -> str:
    s = (ans or "").strip()
    if keep_newlines:
        s = re.sub(r"[ \t\f\v]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
    else:
        s = re.sub(r"\s+", " ", s)

    if len(s) <= limit:
        return force_zh_period(s)

    cut = s[:limit]
    last_nl = cut.rfind("\n")
    last_punc = max(cut.rfind(ch) for ch in "。！？；;")
    pos = max(last_nl, last_punc)
    if pos > int(limit * 0.4):
        cut = cut[:pos + 1]
    else:
        cut = cut.rstrip()
        cut = cut.strip()

    # 不再另外判斷各種標點，統一用強制句號
    return force_zh_period(cut)


# ★ fallback 也走統一處理
def safe_send_reply(api: MessagingApi, reply_token: str, messages: list):
    try:
        api.reply_message(ReplyMessageRequest(replyToken=reply_token, messages=messages))
    except ApiException as e:
        logging.error("[LINE ApiException] status=%s body=%s", getattr(e, "status", "?"), getattr(e, "body", e))
        fallback_text = None
        for m in messages:
            if isinstance(m, TextMessage):
                fallback_text = m.text
                break
        if not fallback_text:
            fallback_text = "卡片發送失敗，改用文字回覆。"
        try:
            safe_text = prepare_line_text(fallback_text, 500, keep_newlines=True)
            api.reply_message(ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=safe_text)]
            ))
        except Exception:
            logging.exception("[LINE Fallback] text reply also failed")

def flex_advisor_card(title: str, summary: str = "") -> TemplateMessage:
    """
    精簡顧問卡片：固定描述文字，使用標題與預約按鈕。
    """
    thumb = urljoin(request.url_root, "static/brand/palsys.png")
    thumb += f"?v={int(time.time())}"

    title_safe = (title or "產品選型建議")[:40]

    # ✅ 寫死文字內容（固定顯示）
    fixed_text = "企業級 AI、雲端與資料解決方案。"

    return TemplateMessage(
        altText="產品選型建議",
        template=ButtonsTemplate(
            thumbnailImageUrl=thumb,
            imageAspectRatio="rectangle",
            imageSize="cover",
            imageBackgroundColor="#F3F4F6",
            title=title_safe,
            text=fixed_text,  # ← 這裡不再依 summary
            actions=[
                URIAction(label="預約 Demo", uri="https://www.palsys.com.tw/#contact"),
            ],
        ),
    )


# --- 最小輔助：偵測是否在問整合/搭配（不動原有 SELECTOR_PAT） ---
_INTEG_HINT = re.compile(r"(結合|整合|搭配|組合|一起用|如何結合|怎麼結合)", re.I)
def _looks_like_integration_question(q: str) -> bool:
    return bool(_INTEG_HINT.search(q or ""))

def _card_title_for(q: str) -> str:
    return "整合／搭配建議" if _looks_like_integration_question(q) else "產品選型建議"

# --- 選型顧問 ---
def try_selector_flow(user_text: str) -> Optional[Tuple[str, TemplateMessage]]:
    if not SELECTOR_PAT.search(user_text or ""):
        return None
    logging.info("[SELECTOR_FLOW] intent matched")

    brand_hits = detect_brands(user_text)
    if _looks_like_integration_question(user_text) and not brand_hits:
        hint = (
            "想給你更精準的整合建議，請指名品牌或產品，例如：\n"
            "• IBM 和 Qlik 怎麼結合？\n"
            "• Cloudera 跟 MongoDB 的串接建議？\n"
            "• Palo Alto + Splunk 有推薦的搭配嗎？"
        )
        # ★ 同樣先過自然截斷
        hint = prepare_line_text(hint, limit=480, keep_newlines=True)
        return hint, None

    q = canonicalize_query(user_text)
    if brand_hits:
        q = f"{q} {' '.join(brand_hits)} integration combine 整合 搭配 建議"

    cand = hybrid_retrieve(q, out_k=INITIAL_K)
    top = rerank_with_crossencoder(q, cand, top_k=RERANK_TOP_K)
    raw_ans = advisor_answer(user_text, top)
    logging.info("[SELECTOR_FLOW] generated advisor answer, len=%d", len(raw_ans))

    # ★ 出站前統一處理
    ans = prepare_line_text(to_trad_tw(raw_ans), limit=480, keep_newlines=True)
    return ans, flex_advisor_card(_card_title_for(user_text), ans)

# =========================================================
# 4) Flask + LINE Webhook + ProxyFix
# =========================================================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

@app.get("/health")
def health():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")
    return "OK", 200

# =========================================================
# 5) Brand / Model → Image mapping（含健檢）
# =========================================================
THANKS_PAT = re.compile(r'(謝謝|感謝|多謝|謝啦|謝了|感恩|thank\s*you|thanks|thx|3q)', re.I)

BRAND_ALIASES = {
    "ibm":       [r"ibm", r"^ibm\s+watson", r"watsonx(?:\.ai|\.data|\.governance)?", r"國際商業機器"],
    "mongodb":   [r"mongo\s*db", r"mongodb"],
    "paloalto":  [r"palo\s*alto", r"paloalto", r"^pa-(ngfw|fw|series)?", r"帕洛阿爾托"],
    "qlik":      [r"qlik"],
    "splunk":    [r"splunk"],
    "suse":      [r"suse", r"opensuse"],
    "synopsys":  [r"synopsys"],
    "tibco":     [r"tibco"],
    "cloudera":  [r"\bcloudera\b", r"\bcdp\b", r"cloudera\s*data\s*platform", r"cloudera\s*manager", r"cloudera\s*machine\s*learning", r"\bcdh\b", r"hortonworks", r"克勞德拉"],
    "cloudcasa": [r"cloudcasa", r"cloud\s*casa", r"catalogic\s*cloudcasa", r"雲端備份", r"雲端casa"],
    "sas":       [r"\bsas\b", r"sas\s*institute", r"statistical\s*analysis\s*system", r"統計分析系統"],
    "instana":   [r"instana", r"ibm\s*instana", r"應用程式監控", r"apm", r"可觀測性", r"observability"],
}

BRAND_IMAGE_FILE = {
    "ibm": "ibm.png",
    "mongodb": "mongodb.png",
    "paloalto": "paloalto.png",
    "qlik": "qlik.png",
    "splunk": "splunk.png",
    "suse": "suse.png",
    "synopsys": "synopsys.png",
    "tibco": "tibco.png",
    "cloudera": "cloudera.png",
    "cloudera cml": "cloudera.png",
    "cloudera cdp": "cloudera.png",
    "cloudcasa": "cloudcasa.png",
    "sas": "sas.png",
    "instana": "INSTANA.png"
}

MODEL_IMAGE_FILE = {
    "S1012": "IBM-Power-S1012.png",
    "S1014": "IBM-Power-S1014.png",
    "S1022": "IBM-Power-S1022.png",
    "S1022s": "IBM-Power-S1022s.png",
    "L1022": "IBM-Power-L1022.png",
    "S1024": "IBM-Power-S1024.png",
    "L1024": "IBM-Power-L1024.png",
    "E1050": "IBM-Power-E1050.png",
    "E1080": "IBM-Power-E1080.png",
}

MODEL_PAT = re.compile(r'(?<![A-Za-z0-9])([ESL][0-9]{4}s?)(?![A-Za-z0-9])', re.I)

def _to_ascii(s: str) -> str:
    if not s:
        return s
    table = {ord('Ｓ'): 'S', ord('Ｌ'): 'L', ord('Ｅ'): 'E'}
    for i in range(10):
        table[ord(chr(0xFF10 + i))] = str(i)
    for i in range(26):
        table[ord(chr(0xFF21 + i))] = chr(0x41 + i)
        table[ord(chr(0xFF41 + i))] = chr(0x61 + i)
    return s.translate(table)

def detect_brands(user_text: str) -> list[str]:
    s = (user_text or "").lower().strip()
    s_no_space = re.sub(r"\s+", "", s)
    hits = []
    for key, patterns in BRAND_ALIASES.items():
        for pat in patterns:
            if re.search(pat, s) or re.search(pat, s_no_space):
                hits.append(key)
                break
    return hits

def detect_models(user_text: str) -> list[str]:
    txt = _to_ascii(user_text or "")
    hits = MODEL_PAT.findall(txt)
    out, seen = [], set()
    for h in hits:
        u = h.upper()
        u = "S1022s" if u == "S1022S" else u
        if u not in seen:
            seen.add(u)
            out.append(u)
    logging.info(f"[IMAGE] detected models: {out}")
    return out

def _is_https(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith("https://")

def _head_ok(url: str) -> bool:
    try:
        r = requests.head(url, timeout=3, allow_redirects=True)
        ct = (r.headers.get("Content-Type", "") or "").lower()
        ok = (200 <= r.status_code < 300) and ct.startswith("image/")
        logging.info(f"[IMAGE] HEAD {url} -> {r.status_code} {ct}")
        return ok
    except Exception as e:
        logging.warning(f"[IMAGE] HEAD fail {url}: {e}")
        return False

def _ensure_trailing_slash(base: str) -> str:
    return base if base.endswith("/") else (base + "/")

def _choose_base(fallback_from_request: bool, preview: bool) -> str | None:
    base_env = (BRAND_PREVIEW_BASE_URL if preview else BRAND_ASSET_BASE_URL).strip()
    if _is_https(base_env):
        return _ensure_trailing_slash(base_env)
    if not fallback_from_request:
        return None
    base = urljoin(request.url_root, "static/brand/")
    if _is_https(base):
        return _ensure_trailing_slash(base)
    logging.warning(f"[IMAGE] fallback base is not HTTPS: {base!r}")
    return None

def _append_ver(url: str) -> str:
    if not url:
        return url
    return f"{url}{'&' if '?' in url else '?'}v={int(time.time())}"

def build_brand_images(brand_keys: list[str], limit: int = 1):
    msgs = []
    if not brand_keys:
        return msgs
    brand_keys = [(b or "").lower() for b in brand_keys]
    allow_req_fallback = (request is not None)
    base_preview = _choose_base(fallback_from_request=allow_req_fallback, preview=True)
    base_origin  = _choose_base(fallback_from_request=allow_req_fallback, preview=False)
    if not base_preview and not base_origin and not _is_https(PLACEHOLDER_IMAGE_URL):
        logging.warning("[IMAGE] 無可用 HTTPS 基底，且未設定 PLACEHOLDER_IMAGE_URL，跳過送圖。")
        return msgs
    sent_any = False
    for b in brand_keys[:limit]:
        fname = BRAND_IMAGE_FILE.get(b.lower())
        if not fname:
            logging.info(f"[IMAGE] no mapping for brand_key={b!r}")
            continue
        preview_url = urljoin(base_preview, fname) if base_preview else ""
        origin_url  = urljoin(base_origin,  fname) if base_origin  else ""
        if not preview_url:
            preview_url = origin_url
        if not origin_url:
            origin_url = preview_url
        preview_url = _append_ver(preview_url)
        origin_url  = _append_ver(origin_url)
        ok_preview = _is_https(preview_url) and _head_ok(preview_url)
        ok_origin  = _is_https(origin_url)  and _head_ok(origin_url)
        if not ok_origin and ok_preview:
            origin_url = preview_url
            ok_origin = True
            logging.info(f"[IMAGE] downgrade: use preview for original ({origin_url})")
        if not ok_preview or not ok_origin:
            logging.warning(f"[IMAGE] brand={b} not available (preview_ok={ok_preview}, origin_ok={ok_origin})")
            continue
        msgs.append(ImageMessage(
            original_content_url=origin_url,
            preview_image_url=preview_url
        ))
        sent_any = True
    if not sent_any and _is_https(PLACEHOLDER_IMAGE_URL) and _head_ok(PLACEHOLDER_IMAGE_URL):
        u = _append_ver(PLACEHOLDER_IMAGE_URL)
        msgs.append(ImageMessage(original_content_url=u, preview_image_url=u))
    return msgs

def build_model_images(models: list[str], limit: int = 1):
    msgs = []
    if not models:
        return msgs
    allow_req_fallback = (request is not None)
    base_preview = _choose_base(fallback_from_request=allow_req_fallback, preview=True)
    base_origin  = _choose_base(fallback_from_request=allow_req_fallback, preview=False)
    if not base_preview and not base_origin and not _is_https(PLACEHOLDER_IMAGE_URL):
        logging.warning("[IMAGE] 無可用 HTTPS 基底，且未設定 PLACEHOLDER_IMAGE_URL，跳過送圖。")
        return msgs
    sent_any = False
    for m in models[:limit]:
        fname = MODEL_IMAGE_FILE.get(m)
        if not fname:
            continue
        preview_url = urljoin(base_preview, fname) if base_preview else ""
        origin_url  = urljoin(base_origin,  fname) if base_origin  else ""
        if not preview_url:
            preview_url = origin_url
        if not origin_url:
            origin_url = preview_url
        preview_url = _append_ver(preview_url)
        origin_url  = _append_ver(origin_url)
        ok_preview = _is_https(preview_url) and _head_ok(preview_url)
        ok_origin  = _is_https(origin_url)  and _head_ok(origin_url)
        if not ok_origin and ok_preview:
            origin_url = preview_url
            ok_origin = True
            logging.info(f"[IMAGE] model downgrade: use preview for original ({origin_url})")
        if not ok_preview or not ok_origin:
            logging.warning(f"[IMAGE] model={m} not available (preview_ok={ok_preview}, origin_ok={ok_origin})")
            continue
        msgs.append(ImageMessage(
            original_content_url=origin_url,
            preview_image_url=preview_url
        ))
        sent_any = True
    if not sent_any and _is_https(PLACEHOLDER_IMAGE_URL) and _head_ok(PLACEHOLDER_IMAGE_URL):
        u = _append_ver(PLACEHOLDER_IMAGE_URL)
        msgs.append(ImageMessage(original_content_url=u, preview_image_url=u))
    return msgs

def official_site_button() -> TemplateMessage:
    thumb = urljoin(request.url_root, "static/brand/palsys.png")
    thumb += f"?v={int(time.time())}"
    return TemplateMessage(
        altText="Palsys 官網",
        template=ButtonsTemplate(
            thumbnailImageUrl=thumb,
            imageAspectRatio="rectangle",
            imageSize="cover",
            imageBackgroundColor="#F3F4F6",
            title="🌐 Palsys 官方網站",
            text="資安・數據・軟體・雲端整合服務",
            actions=[
                URIAction(label="前往官網", uri="https://www.palsys.com.tw/"),
            ]
        )
    )

# ---- 文字訊息處理 ----
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    try:
        user_text = (event.message.text or "").strip()

        # 1) 官網按鈕（維持原行為）
        if _is_official_site_intent(user_text):
            with ApiClient(line_config) as api_client:
                api = MessagingApi(api_client)
                safe_send_reply(api, event.reply_token, [official_site_button()])
            return

        # 2) 簡單打招呼/空字串
        if not user_text:
            reply_text = "請輸入文字問題。"
        elif THANKS_PAT.search(user_text):
            reply_text = "不用客氣！"
        elif user_text.lower() in {"hi","hello","嗨","哈囉","在嗎","你好","ok"}:
            reply_text = "你好,歡迎使用我們的服務!"
        else:
            # 3) 先走「選型顧問」
            tried = try_selector_flow(user_text)
            if tried:
                ans, card = tried
                msgs = [TextMessage(text=prepare_line_text(to_trad_tw(ans), 500, keep_newlines=True))]  # ★ 統一處理
                if card:
                    msgs.append(card)
                with ApiClient(line_config) as api_client:
                    api = MessagingApi(api_client)
                    safe_send_reply(api, event.reply_token, msgs)
                return

            # 4) 沒命中顧問 → 走 RAG
            reply_text = rag_answer(user_text)

        # 5) 統一文字處理（繁轉台灣＋自然截斷）
        reply_text = to_trad_tw((reply_text or "").strip()) or "抱歉，我沒有找到足夠資訊，能再提供更多關鍵字嗎？"
        reply_text_processed = prepare_line_text(reply_text, limit=1800, keep_newlines=True)  # ★ 修正原本 eply_text 筆誤

        # 6) 品牌/型號圖
        hit_brands = detect_brands(user_text)
        image_msgs_brand = build_brand_images(hit_brands, limit=1)

        hit_models = detect_models(user_text)
        image_msgs_model = build_model_images(hit_models, limit=1)

        # 7) 回覆
        with ApiClient(line_config) as api_client:
            api = MessagingApi(api_client)
            safe_send_reply(api, event.reply_token, [TextMessage(text=reply_text_processed), *image_msgs_brand, *image_msgs_model])

    except Exception:
        logging.exception("[HANDLE_TEXT] unexpected error")
        with ApiClient(line_config) as api_client:
            api = MessagingApi(api_client)
            safe_send_reply(api, event.reply_token, [TextMessage(text="系統小當機，我們已記錄錯誤並會修復，請稍後再試。")])

# =========================================================
# 6) 貼圖處理
# =========================================================
STICKER_POOL = [
    ("11537", "52002745"),
    ("11537", "52002768"),
    ("11537", "52002739"),
    ("11538", "51626518"),
    ("6362",  "11087922"),
    ("6362",  "11087924"),
]

@handler.add(MessageEvent, message=StickerMessageContent)
def handle_sticker_message(event: MessageEvent):
    pk, sid = random.choice(STICKER_POOL)
    with ApiClient(line_config) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[StickerMessage(packageId=pk, stickerId=sid)]
            )
        )

enable_fallback(globals())
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
