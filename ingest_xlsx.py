# -*- coding: utf-8 -*-
"""
ingest_xlsx.py  (e5 版；支援 .txt 與 Excel 檔或資料夾；具去重；可安全 append)
- Excel：每張表三欄 [項目, 內容, 圖片/代碼]，以「內容」為主
- TXT：讀取 TXT_DIR 下所有 .txt（遞迴）；標題預設首行（或檔名）
- 產出/更新：rag_index/md_chunks.faiss + rag_index/md_meta.parquet
- REBUILD_MODE=True  → 僅用本次資料重建（覆蓋舊索引/中繼）
- REBUILD_MODE=False → 僅把「新片段」append 到既有索引/中繼（不會重複）

重點：
1) 使用 e5（預設 intfloat/multilingual-e5-base），索引端嵌入一律加 "passage: "（查詢端請用 "query: "）
2) Excel 路徑可指向「單一檔案」或「資料夾」（會遞迴掃 .xlsx/.xlsm/.xls）
3) append 前檢查 FAISS 維度；不一致請改 REBUILD_MODE=True 重建
"""
import os, re, hashlib
from pathlib import Path
from typing import List, Dict

import numpy as np
import pandas as pd
import faiss
import pyarrow as pa
import pyarrow.parquet as pq
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# ---------------- 路徑與開關 ----------------
BASE = Path(__file__).resolve().parent

# 切換資料來源（也可用環境變數覆蓋）
READ_XLSX   = os.getenv("READ_XLSX", "false").lower() in {"1","true","yes","y"}
READ_TXT    = os.getenv("READ_TXT",  "true").lower()  in {"1","true","yes","y"}

# Excel 可指向單一檔案或資料夾（相對於本檔案）
EXCEL_PATH  = Path(os.getenv("EXCEL_PATH", str(BASE / "Additional information" )))
SHEET_NAMES = None  # None=全部工作表；或指定 ["Sheet1", "Sheet2"]

# TXT 來源資料夾（遞迴）
TXT_DIR     = Path(os.getenv("TXT_DIR", str(BASE / "txt_docs")))

# ---------------- 建索引模式 ----------------
REBUILD_MODE = os.getenv("REBUILD_MODE", "false").lower() in {"1","true","yes","y"}

# ---------------- Embedding 與輸出 ----------------
MODEL_NAME = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-base")
BATCH      = int(os.getenv("BATCH", "64"))
OUT_DIR    = Path(os.getenv("OUT_DIR", str(BASE / "rag_index")))
INDEX_PATH = OUT_DIR / "md_chunks.faiss"
META_PATH  = OUT_DIR / "md_meta.parquet"

# ---------------- 切塊參數 ----------------
MAX_CHARS = int(os.getenv("MAX_CHARS", "800"))
OVERLAP   = int(os.getenv("OVERLAP", "100"))

# ---------------- 基礎工具 ----------------
def clean_text(s: str) -> str:
    if not isinstance(s, str):
        s = "" if pd.isna(s) else str(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def chunk_text(text: str, max_chars: int = 800, overlap: int = 100):
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    out, start, L = [], 0, len(text)
    while start < L:
        end = min(L, start + max_chars)
        out.append(text[start:end])
        if end == L:
            break
        start = max(0, end - overlap)
    return out

# ---- e5 索引端一律加 "passage: " 前綴 ----
def fmt_passage(t: str) -> str:
    t = (t or "").strip()
    return f"passage: {t}" if t else ""

def embed_texts(model: SentenceTransformer, texts: List[str], batch: int) -> np.ndarray:
    vecs = []
    passages = [fmt_passage(t) for t in texts]
    for i in tqdm(range(0, len(passages), batch), desc="Embedding (e5 + passage:)"):
        part = model.encode(
            passages[i:i+batch],
            convert_to_numpy=True,
            normalize_embeddings=True
        )
        vecs.append(part)
    if not vecs:
        return np.empty((0, model.get_sentence_embedding_dimension()), dtype="float32")
    return np.vstack(vecs).astype("float32")

def write_meta(df: pd.DataFrame):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), str(META_PATH))

def read_meta() -> pd.DataFrame:
    if META_PATH.exists():
        return pq.read_table(str(META_PATH)).to_pandas()
    return pd.DataFrame(columns=["vendor","doc_name","chunk_id","title","text","digest"])

def load_or_create_index(dim: int) -> faiss.Index:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if INDEX_PATH.exists():
        idx = faiss.read_index(str(INDEX_PATH))
        # 維度需一致
        if hasattr(idx, "d") and idx.d != dim:
            raise ValueError(
                f"FAISS 維度不符（index.d={idx.d} vs new_emb_dim={dim}）。"
                "代表舊索引用了不同嵌入模型，請改 REBUILD_MODE=True 重新建立。"
            )
        return idx
    return faiss.IndexFlatIP(dim)

# ---- 去重用雜湊 ----
def text_norm_for_hash(s: str) -> str:
    s = clean_text(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def make_digest(vendor: str, doc_name: str, title: str, text: str) -> str:
    # 如果想「跨檔案也視為重複」，改成只用 text_norm_for_hash(text)
    base = f"{(vendor or '').strip()}|{(doc_name or '').strip()}|{(title or '').strip()}|{text_norm_for_hash(text)}"
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()

def ensure_meta_digest(df: pd.DataFrame) -> pd.DataFrame:
    if "digest" not in df.columns:
        df = df.copy()
        df["digest"] = [
            make_digest(r.get("vendor",""), r.get("doc_name",""), r.get("title",""), r.get("text",""))
            for _, r in df.iterrows()
        ]
    return df

# ---------------- Excel 讀取 ----------------
def _rows_from_excel_df(df: pd.DataFrame, vendor: str, doc_name: str, sheet_name: str) -> List[Dict]:
    rows = []
    for i in range(len(df)):
        item = df.iloc[i, 0] if 0 in df.columns else None
        content = df.iloc[i, 1] if 1 in df.columns else None
        if i == 0 and (pd.isna(item) and pd.isna(content)):
            continue
        if isinstance(content, str) and content.strip():
            title = f"{vendor} / {sheet_name}｜{item}" if (isinstance(item, str) and item.strip()) else f"{vendor} / {sheet_name}"
            rows.append({
                "vendor": vendor,
                "doc_name": doc_name,
                "title": title.strip(),
                "text": content,
            })
    return rows

def load_excel_source(path_or_dir: Path, names=None) -> pd.DataFrame:
    """path_or_dir 可是單一 Excel 檔或資料夾（遞迴掃 .xlsx/.xlsm/.xls）。"""
    if not READ_XLSX:
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])
    if not path_or_dir.exists():
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])

    files: List[Path] = []
    if path_or_dir.is_file():
        files = [path_or_dir]
    else:
        files = sorted([*path_or_dir.rglob("*.xlsx"), *path_or_dir.rglob("*.xlsm"), *path_or_dir.rglob("*.xls")])

    rows: List[Dict] = []
    for f in files:
        try:
            xls = pd.ExcelFile(str(f))
            sheets = names or xls.sheet_names
            vendor = f.stem  # 以檔名當 vendor；更清楚來源
            doc_name = re.sub(r"\s+", "_", f.stem.strip()).lower() or "sheet"
            for name in sheets:
                df = pd.read_excel(str(f), sheet_name=name, header=None)
                rows.extend(_rows_from_excel_df(df, vendor, doc_name, name))
        except Exception as e:
            print(f"[WARN] Skip Excel {f}: {e}")

    if not rows:
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])
    return pd.DataFrame(rows)

# ---------------- TXT 讀取 ----------------
def read_txt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        try:
            return path.read_text(encoding="cp950", errors="ignore")
        except Exception:
            return path.read_text(encoding="utf-8", errors="ignore")

def load_txt_dir(txt_dir: Path) -> pd.DataFrame:
    if not READ_TXT:
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])
    if not txt_dir.exists():
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])

    rows = []
    for p in txt_dir.rglob("*.txt"):
        try:
            raw = read_txt(p)
            raw = clean_text(raw)
            if not raw:
                continue
            # 標題：首個非空行；找不到就用檔名
            first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
            title = first_line if first_line else p.stem
            # vendor：用上一層資料夾名（沒有就 'txt'）
            vendor = (p.parent.name or "txt").strip()
            doc_name = re.sub(r"\s+", "_", p.stem.strip()).lower() or "doc"
            rows.append({
                "vendor": vendor,
                "doc_name": doc_name,
                "title": title,
                "text": raw,
            })
        except Exception as e:
            print(f"[WARN] Skip TXT {p}: {e}")
            continue

    if not rows:
        return pd.DataFrame(columns=["vendor","doc_name","title","text"])
    return pd.DataFrame(rows)

# ---------------- 切塊 ----------------
def explode_to_chunks(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for _, r in df.iterrows():
        vendor   = r["vendor"]
        doc_name = r["doc_name"]
        title    = r["title"]
        for i, ch in enumerate(chunk_text(r["text"], MAX_CHARS, OVERLAP)):
            out.append({
                "vendor": vendor,
                "doc_name": doc_name,
                "chunk_id": i,
                "title": title,
                "text": ch,
                "digest": make_digest(vendor, doc_name, title, ch),
            })
    return pd.DataFrame(out, columns=["vendor","doc_name","chunk_id","title","text","digest"])

# ---------------- 主流程 ----------------
def main():
    print(f"[INFO] Load model (e5): {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    # 讀 Excel（檔或資料夾）+ TXT，依旗標合併
    print(f"[INFO] READ_XLSX={READ_XLSX} | Excel source: {EXCEL_PATH.resolve()} (exists={EXCEL_PATH.exists()})")
    raw_xlsx = load_excel_source(EXCEL_PATH, SHEET_NAMES)

    print(f"[INFO] READ_TXT={READ_TXT} | TXT dir: {TXT_DIR.resolve()} (exists={TXT_DIR.exists()})")
    raw_txt = load_txt_dir(TXT_DIR)

    raw_list = []
    if not raw_xlsx.empty:
        print(f"[INFO] Excel rows: {len(raw_xlsx)}")
        raw_list.append(raw_xlsx)
    if not raw_txt.empty:
        print(f"[INFO] TXT rows:   {len(raw_txt)}")
        raw_list.append(raw_txt)

    if not raw_list:
        print("[WARN] No rows parsed from Excel/TXT. Nothing to do.")
        return

    raw = pd.concat(raw_list, ignore_index=True)
    chunks = explode_to_chunks(raw)
    if chunks.empty:
        print("[WARN] No chunks produced.")
        return

    # 同一批次內也去一次重（避免來源檔重複內容）
    chunks = chunks.drop_duplicates(subset=["digest"]).reset_index(drop=True)

    print(f"[INFO] Total docs: {len(raw)} | Chunks(after local dedup): {len(chunks)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if REBUILD_MODE:
        # 全量嵌入 → 重建索引/中繼
        embs = embed_texts(model, chunks["text"].tolist(), BATCH)
        dim = embs.shape[1] if len(embs) > 0 else model.get_sentence_embedding_dimension()
        idx = faiss.IndexFlatIP(dim)
        if len(embs) > 0:
            idx.add(embs)
        faiss.write_index(idx, str(INDEX_PATH))
        write_meta(chunks)
        print(f"✅ Rebuilt | index={idx.ntotal} dim={dim} | meta rows={len(chunks)}")
    else:
        # 僅加入「新片段」
        idx = load_or_create_index(model.get_sentence_embedding_dimension())
        old_meta = ensure_meta_digest(read_meta())
        old_digests = set(old_meta["digest"].tolist())

        to_add = chunks[~chunks["digest"].isin(old_digests)].reset_index(drop=True)
        if to_add.empty:
            print("✅ Nothing new. All chunks already indexed. (skip embedding)")
            # 仍確保 meta 具備 digest 欄位
            write_meta(old_meta)
            faiss.write_index(idx, str(INDEX_PATH))
            return

        print(f"[INFO] New chunks to add: {len(to_add)}")
        embs = embed_texts(model, to_add["text"].tolist(), BATCH)
        if len(embs) > 0:
            idx.add(embs)
        faiss.write_index(idx, str(INDEX_PATH))

        new_meta = pd.concat([old_meta, to_add], ignore_index=True)
        write_meta(new_meta)
        print(f"✅ Appended | added chunks={len(to_add)} | index total={idx.ntotal} | meta rows={len(new_meta)}")

if __name__ == "__main__":
    main()
