import os, numpy as np, pandas as pd, faiss
from tqdm import tqdm
import pyarrow.parquet as pq
from sentence_transformers import SentenceTransformer

PARQUET = "chunks.parquet"
MODEL = "intfloat/multilingual-e5-base"   # ← 改用 multilingual e5
OUT_DIR = "rag_index"
BATCH = 48  # e5 比 MiniLM 大，不夠記憶體就再降到 32

os.makedirs(OUT_DIR, exist_ok=True)
print("Loading parquet...")
df = pq.read_table(PARQUET).to_pandas()

print("Loading model:", MODEL)
model = SentenceTransformer(MODEL)

texts = df["text"].fillna("").tolist()

# ★ e5 規範：文件用 "passage: " 前綴（查詢端要用 "query: "）
to_encode = [f"passage: {t}" for t in texts]

emb_list = []
for i in tqdm(range(0, len(to_encode), BATCH), desc="Embedding"):
    emb = model.encode(
        to_encode[i:i+BATCH],
        convert_to_numpy=True,
        normalize_embeddings=True
    )
    emb_list.append(emb)
emb = np.vstack(emb_list).astype("float32")

# cosine = 內積（因為已經 L2-normalize）
index = faiss.IndexFlatIP(emb.shape[1])
index.add(emb)
faiss.write_index(index, os.path.join(OUT_DIR, "md_chunks.faiss"))
df.to_parquet(os.path.join(OUT_DIR, "md_meta.parquet"), index=False)
print("✅ index size:", index.ntotal, "dims:", emb.shape[1])
