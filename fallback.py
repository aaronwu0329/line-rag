# -*- coding: utf-8 -*-
import os, re, json, logging, inspect
from dataclasses import dataclass

@dataclass
class OAIConfig:
    min_local_score: float = 0.32
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    timeout_sec: float = 15.0
    use_responses_api: bool = False

_SYS_PROMPT = (
    "你是企業文件助理。僅輸出一段精簡的繁體中文回覆；"
    "絕對禁止使用簡體字；禁止條列、步驟、分析、原答案、參考答案、Answer:、Output、References 等字樣；"
    "不要反問或要求更多資訊；不要重述或改寫題目；"
    "題目過短或僅為名詞時，直接給出 2–4 句的簡短介紹（定位、用途、重點特色），且以陳述句結尾。"
)

def _http_post_json(url, headers, payload, timeout):
    import requests
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    return r.json()

def _call_openai_chat(prompt: str, cfg: OAIConfig) -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 OPENAI_API_KEY")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    base = os.getenv("OPENAI_BASE_URL", cfg.base_url).rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": os.getenv("OPENAI_MODEL", cfg.model),
        "messages": [
            {"role": "system", "content": _SYS_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": int(os.getenv("OPENAI_MAX_NEW_TOKENS", "220")),
        "presence_penalty": 0.0,
        "frequency_penalty": 0.2,
        "stop": ["Answer:", "Step-by-step", "Output"],
    }
    j = _http_post_json(url, headers, payload, timeout=cfg.timeout_sec)
    try:
        return (j["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return json.dumps(j)[:1000]

def _flag_enabled() -> bool:
    v1 = os.getenv("ENABLE_FALLBACK", "")
    v2 = os.getenv("ENABLE_OPENAI_FALLBACK", "")
    def is_on(v: str) -> bool: return v.lower() in {"1","true","yes","on"}
    return is_on(v1) or is_on(v2)

def enable_fallback(env: dict, *, config: OAIConfig | None = None):
    if not _flag_enabled():
        logging.info("[OAI] fallback disabled")
        return

    # 不用記憶 → 拿掉 MEM / rewrite_with_history 需求
    required = [
        "rag_answer","try_company_or_brand_list",
        "normalize_whitespace","detect_models","detect_brands","looks_like_gibberish","CLARIFY_MSG",
        "canonicalize_query","autocomplete_weak_query","expand_spec_synonyms",
        "hybrid_retrieve","rerank_with_crossencoder","build_context",
        "_finalize","RELEVANCE_TH","MAX_CTX_CHARS","_log_top5_after_rerank","RERANKER_NAME",
        "call_llm",
    ]
    for k in required:
        if k not in env or env[k] is None:
            raise RuntimeError(f"[OAI] 缺少必要符號：{k}")

    # 盡量沿用你主程式中的工具
    try_company_or_brand_list = env["try_company_or_brand_list"]
    normalize_whitespace      = env["normalize_whitespace"]
    detect_models             = env["detect_models"]
    detect_brands             = env["detect_brands"]
    looks_like_gibberish      = env["looks_like_gibberish"]
    CLARIFY_MSG               = env["CLARIFY_MSG"]
    canonicalize_query        = env["canonicalize_query"]
    autocomplete_weak_query   = env["autocomplete_weak_query"]
    expand_spec_synonyms      = env["expand_spec_synonyms"]
    hybrid_retrieve           = env["hybrid_retrieve"]
    rerank_with_crossencoder  = env["rerank_with_crossencoder"]
    build_context             = env["build_context"]
    _finalize_fn              = env["_finalize"]
    RELEVANCE_TH              = env["RELEVANCE_TH"]
    MAX_CTX_CHARS             = env["MAX_CTX_CHARS"]
    _log_top5_after_rerank    = env["_log_top5_after_rerank"]
    RERANKER_NAME             = env["RERANKER_NAME"]
    call_llm                  = env["call_llm"]

    # 指令化工具：優先用你已有的；沒有就內建一個（不依賴記憶）
    _as_instr = env.get("_as_instruction_for_prompt") or env.get("_as_question_for_prompt") or (
        lambda q: (q.strip() + "？") if q and not re.search(r'[。!?？]$', q.strip()) else (q.strip() or "請簡要說明。")
    )

    cfg = config or OAIConfig(
        min_local_score=float(os.getenv("OAI_MIN_LOCAL_SCORE", str(RELEVANCE_TH))),
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout_sec=float(os.getenv("OPENAI_TIMEOUT_SEC", "15")),
        use_responses_api=os.getenv("OPENAI_USE_RESPONSES","0") in {"1","true","yes","on"}
    )

    def _best_score(rows):
        if not rows: return 0.0
        r0 = rows[0]
        return float(r0.get("rerank_score", r0.get("score", 0.0)) or 0.0)

    # —— 無記憶版本：每次只用「本句」查詢 ——
    def _answer_no_memory(user_query: str) -> str:
        pre = try_company_or_brand_list(user_query)
        if pre:
            return pre

        q_raw = normalize_whitespace(user_query)
        hit_models = detect_models(q_raw)
        hit_brands = detect_brands(q_raw)
        if looks_like_gibberish(q_raw, hit_models, hit_brands):
            return CLARIFY_MSG

        q_norm   = canonicalize_query(q_raw)
        boosted  = autocomplete_weak_query(q_norm)
        if hit_models:
            boosted = f"IBM Power {hit_models[0]} {boosted}".strip()
        boosted  = expand_spec_synonyms(boosted, hit_models)

        local_candidates = hybrid_retrieve(boosted, out_k=10)
        top_rows         = rerank_with_crossencoder(user_query, local_candidates, top_k=8)
        try:
            _log_top5_after_rerank(user_query, top_rows, RERANKER_NAME)
        except Exception: pass

        best_score = _best_score(top_rows)
        logging.info(f"[OAI] best_local_score={best_score:.3f} (threshold={cfg.min_local_score:.3f})")

        if top_rows and best_score >= cfg.min_local_score:
            ctx, _ = build_context(top_rows, max_chars=MAX_CTX_CHARS)
            prompt = (
                "以下是可用的背景片段文本（不要在回答中提到片段或 Chunk）：\n"
                f"{ctx}\n"
                "請根據以上內容，用一段精簡的繁體中文回答下列問題；使用陳述句結尾：\n"
                f"{_as_instr(user_query)}\n"
            )
            logging.info("[OAI] USING_OPENAI=False (use watsonx with local context)")
            ans = call_llm(prompt)
            return ans[:1900]

        # 低信心 → OpenAI
        if top_rows:
            ctx, _ = build_context(top_rows[:2], max_chars=min(1000, MAX_CTX_CHARS//2))
            prompt = (
                "以下是可能相關的背景片段（不要在回答中提到來源）：\n"
                f"{ctx}\n"
                "若片段仍不足，請直接依你所知用 2–4 句作答，使用陳述句結尾：\n"
                f"{_as_instr(user_query)}\n"
            )
        else:
            prompt = (
                "知識庫裡沒有足夠相關內容。請直接用 2–4 句作答（定位、用途、重點特色），"
                "不要反問或請求更多資訊，並以陳述句結尾。\n"
                f"{_as_instr(user_query)}\n"
            )

        try:
            logging.info(f"[OAI] USING_OPENAI=True (model={cfg.model})")
            raw = _call_openai_chat(prompt, cfg)
            ans = _finalize_fn(raw)
            return ans[:1900]
        except Exception as e:
            logging.error(f"[OAI] fallback failed: {e}")
            logging.info("[OAI] USING_OPENAI=False (fallback back to watsonx)")
            prompt2 = (
                "知識庫裡沒有找到足夠相關的內容。請你直接作答，不要反問或請求更多資訊；"
                "若題目只有名詞或像「介紹 X」「什麼是 X」，請用 2–4 句精簡介紹並以陳述句結尾。\n"
                f"{_as_instr(user_query)}\n"
            )
            ans = call_llm(prompt2)
            return ans[:1900]

    # 兼容你現有的 rag_answer 簽名
    orig = env["rag_answer"]
    try:
        arity = len(inspect.signature(orig).parameters)
    except Exception:
        arity = 1

    if arity == 1:
        def patched_rag_answer(user_query: str) -> str:
            return _answer_no_memory(user_query)
    else:
        # 兩參數版本：忽略 user_id
        def patched_rag_answer(user_id: str, user_query: str) -> str:
            return _answer_no_memory(user_query)

    env["rag_answer"] = patched_rag_answer
    logging.info("[OAI] fallback enabled (no-memory mode; low-confidence -> OpenAI GPT)")
