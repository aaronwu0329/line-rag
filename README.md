# LINE RAG Product Assistant

這是一個以 Flask 建立的 LINE Bot 產品問答助理，使用 RAG（Retrieval-Augmented Generation）回答產品、方案與技術資訊問題。系統整合查詢改寫、FAISS 向量搜尋、BM25 關鍵字搜尋、RRF 融合排序、CrossEncoder reranking、IBM watsonx.ai 生成回答，並可在低信心情境下啟用 OpenAI fallback。

## 快速開始

接手工程師可以用以下順序啟動專案：

```bash
pip install -r requirements.txt
cp .env.example .env
```

接著在 `.env` 填入至少四個必要金鑰：

```env
IBM_API_KEY=
IBM_PROJECT_ID=
LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
```

確認索引檔存在：

```text
rag_index/md_chunks.faiss
rag_index/md_meta.parquet
```

啟動服務：

```bash
python rag_cli.py
```

預設服務會監聽：

```text
http://localhost:8000
```

LINE webhook callback path 是：

```text
/callback
```

因此 LINE Developers 後台要設定成公開 HTTPS 網址，例如：

```text
https://your-domain.example/callback
```

如果在本機測試 LINE webhook，需要使用 ngrok、Cloudflare Tunnel 或其他反向代理工具提供公開 HTTPS URL。

## 啟動成功判斷

啟動時會依序載入：

- `.env` 環境變數
- IBM watsonx.ai client
- `rag_index/md_chunks.faiss`
- `rag_index/md_meta.parquet`
- `intfloat/multilingual-e5-base` embedding model
- BM25 corpus
- CrossEncoder reranker
- Flask server

第一次啟動可能會下載 Hugging Face 模型，所以會比較久。若看到 Flask server 開始監聽指定 port，代表服務已啟動。實際 LINE 問答還需要 LINE webhook URL 設定正確，且 IBM watsonx.ai 金鑰有效。

## 模型分工

本專案使用多個模型，各自負責不同階段：

- `openai/gpt-oss-120b`：IBM watsonx.ai 上的回答生成模型，負責根據 RAG context 產生最終回答。
- `intfloat/multilingual-e5-base`：embedding 模型，負責把知識文件與使用者查詢轉成向量，用於 FAISS 檢索。
- `BAAI/bge-reranker-v2-m3`：主要 reranker，負責重新排序 FAISS/BM25/RRF 找到的候選段落。
- `cross-encoder/ms-marco-MiniLM-L-6-v2`：reranker 載入失敗時的 fallback reranker。
- `gpt-4.1-mini`：OpenAI fallback 模型，只有在本機 RAG 信心不足且 fallback 啟用時使用。

因此 `openai/gpt-oss-120b` 不會取代 embedding 或 reranker。檢索階段仍需要專門的 embedding 與 reranking 模型，生成階段才使用 `openai/gpt-oss-120b`。

## 系統總覽

使用者在 LINE 輸入問題後，主程式 `rag_cli.py` 會執行以下流程：

1. Flask webhook 接收 LINE Messaging API 傳入的訊息。
2. 判斷訊息類型：品牌清單、產品圖片、型號查詢、產品導引或一般 RAG 問答。
3. 對使用者問題做查詢正規化與查詢改寫。
4. 使用 FAISS 向量搜尋與 BM25 關鍵字搜尋找候選段落。
5. 使用 RRF 將向量結果與關鍵字結果融合排序。
6. 使用 CrossEncoder reranker 對候選段落重新排序。
7. 通過信心門檻後，將 top chunks 組成 context。
8. 使用 IBM watsonx.ai 依據 context 生成回答。
9. 若本機 RAG 信心不足，且 OpenAI fallback 已啟用，改由 OpenAI fallback 補充回答。
10. 將回答整理為台灣繁體中文後回傳 LINE。

## 查詢正規化與查詢改寫

查詢處理主要在 `rag_cli.py`、`query_normalize.py` 與 `product_faq.py`。

- `normalize_whitespace`：清理多餘空白、全形空白與格式。
- `canonicalize_query`：統一常見產品查詢格式。
- `normalize_brand_case`：修正品牌大小寫與品牌寫法。
- `detect_models`：偵測 IBM Power 型號，例如 S1022、S1024、E1080。
- `detect_brands`：偵測 IBM、Palo Alto、Qlik、Splunk、SUSE、MongoDB、Cloudera、SAS 等品牌。
- `autocomplete_weak_query`：補強過短或語意不足的問題。
- `expand_spec_synonyms`：展開規格同義詞，例如效能、容量、記憶體、CPU、處理器等問法。
- `translate_zh_to_en`：將中文查詢轉成英文關鍵字，提升英文產品文件命中率。

查詢改寫後，系統會把品牌、型號與規格詞補進檢索查詢。例如使用者只問「S1024 記憶體」，系統會補成更完整的 IBM Power 型號與規格查詢，再送進 hybrid retrieval。

## 知識來源與切塊方式

知識庫建立主要由 `ingest_xlsx.py` 負責。

### 來源檔案

- `txt_docs/`：預設會讀取此資料夾底下所有 `.txt`，包含子資料夾。
- `Additional information/`：可讀取 Excel 檔案，支援 `.xlsx`、`.xlsm`、`.xls`。
- `chunks.parquet`：可由 `export_chunks.py` 從 Hive 匯出，或提供給 `build_index.py` 重新建索引。

### TXT 解析規則

- 使用 UTF-8 讀取，失敗時改用 cp950 或忽略錯誤字元。
- 每個 TXT 檔會先做文字清理。
- 第一個非空白行會作為 `title`。
- 上層資料夾名稱會作為 `vendor`。
- 檔名會轉成 `doc_name`。

### Excel 解析規則

- `READ_XLSX=true` 時才會讀 Excel；預設是關閉。
- Excel 路徑可指向單一檔案或資料夾。
- 若是資料夾，會遞迴掃描 `.xlsx`、`.xlsm`、`.xls`。
- 預設讀取所有工作表。
- 每列以前兩欄為主：第一欄作為項目名稱，第二欄作為內容。
- Excel 檔名會作為 `vendor`，工作表名稱會納入 `title`。

### 切塊規則

切塊函式是 `chunk_text`。

- 預設每個 chunk 最大 `800` 字元。
- 預設 chunk overlap 是 `100` 字元。
- 如果原文小於等於 800 字元，會保留為一個 chunk。
- 如果超過 800 字元，會用滑動視窗切成多個 chunk。
- 下一段 chunk 會從上一段結尾往前 100 字元開始，避免答案被切在邊界。

預設參數可用環境變數調整：

```env
MAX_CHARS=800
OVERLAP=100
```

切塊後每筆 metadata 會包含：

```text
vendor, doc_name, chunk_id, title, text, digest
```

`digest` 是用 `vendor + doc_name + title + text` 做 SHA1，用來去重，避免同一段內容重複進入索引。

## Embedding 與 FAISS 索引

Embedding 建立在 `ingest_xlsx.py` 與 `build_index.py`。

### Embedding 模型

預設模型：

```text
intfloat/multilingual-e5-base
```

這是多語言 embedding 模型，適合中文與英文混合的產品文件。

### 文件端 embedding

建立知識庫時，文件 chunk 會先加上 e5 模型建議的 passage prefix：

```text
passage: {chunk text}
```

接著使用 sentence-transformers：

```python
model.encode(..., convert_to_numpy=True, normalize_embeddings=True)
```

重點：

- `normalize_embeddings=True` 會把向量正規化。
- 向量型別會轉成 `float32`。
- 預設 batch size 是 `64`，可用 `BATCH` 調整。

### 查詢端 embedding

執行查詢時，`rag_cli.py` 會使用同一個 embedding 模型：

```text
intfloat/multilingual-e5-base
```

查詢文字會先經過正規化與改寫，再送進 `SentenceTransformer.encode`，同樣使用 `normalize_embeddings=True`。目前查詢端程式直接 encode 改寫後的 query 字串，文件端則使用 `passage:` prefix。

### FAISS 索引

FAISS 使用：

```python
faiss.IndexFlatIP(dim)
```

因為文件向量與查詢向量都有 normalize，所以 inner product 可用來近似 cosine similarity。

輸出檔案：

```text
rag_index/md_chunks.faiss   # FAISS 向量索引
rag_index/md_meta.parquet   # chunk metadata
```

## 索引建立模式

### 使用 ingest_xlsx.py

`ingest_xlsx.py` 可從 TXT 與 Excel 建立索引。

預設：

```env
READ_TXT=true
READ_XLSX=false
REBUILD_MODE=false
EMBED_MODEL=intfloat/multilingual-e5-base
BATCH=64
```

執行：

```bash
python ingest_xlsx.py
```

模式說明：

- `REBUILD_MODE=true`：重新建立整個 FAISS index 與 metadata。
- `REBUILD_MODE=false`：讀取既有 metadata，根據 digest 只 append 新 chunk。

### 使用 build_index.py

`build_index.py` 會從既有 `chunks.parquet` 建立 FAISS index。

它不負責切塊，假設 `chunks.parquet` 裡已經有：

```text
vendor, doc_name, chunk_id, title, text
```

執行：

```bash
python build_index.py
```

### 使用 export_chunks.py

`export_chunks.py` 會從 Hive 匯出資料到 `chunks.parquet`。

預設查詢：

```sql
SELECT vendor, doc_name, chunk_id, title, text
FROM default.markdown_chunks
ORDER BY vendor, doc_name, chunk_id
```

## Hybrid Retrieval 檢索流程

檢索主要在 `rag_cli.py` 的 `hybrid_retrieve`。

### FAISS 向量搜尋

- 使用改寫後的查詢做向量搜尋。
- 中文查詢會額外轉成英文關鍵字再查一次。
- 英文品牌詞、產品詞與型號詞會額外查詢，提高專有名詞命中率。
- 預設每次向量搜尋取 `VEC_K_EACH=10`。

### BM25 關鍵字搜尋

- 使用 `rank-bm25`。
- 文字會經過 `normalize_for_bm25`。
- 中文用 `jieba` 斷詞。
- 英文、數字、型號會用 regex 抽出 token。
- 英文字會同時保留原始大小寫與小寫版本。
- 預設取 `KW_K_EACH=10`。

## RRF 融合排序

向量搜尋與 BM25 搜尋各自產生候選結果後，系統使用 `_rrf_fuse` 做融合。

RRF（Reciprocal Rank Fusion）會依照候選文件在不同檢索器中的排名給分：

```text
RRF score += 1 / (K + rank)
```

此專案使用：

```text
K = 60
ALPHA_VEC = 0.5
```

最後融合分數：

```text
融合分數 = RRF 分數 + ALPHA_VEC * 向量分數 + (1 - ALPHA_VEC) * BM25 分數
```

這代表文件如果同時被 FAISS 與 BM25 找到，通常會比只被單一檢索方式找到更優先。

## CrossEncoder Reranking

RRF 產生候選段落後，系統再用 CrossEncoder reranker 重新排序。

預設 primary reranker：

```text
BAAI/bge-reranker-v2-m3
```

fallback reranker：

```text
cross-encoder/ms-marco-MiniLM-L-6-v2
```

相關參數：

```text
INITIAL_K=10
RERANK_TOP_K=5
RELEVANCE_TH=0.32
RELEVANCE_TH_MODEL=0.18
MAX_CTX_CHARS=2200
```

reranker 會用 `(query, candidate text)` 配對計算相關分數，最後保留最相關的 chunks 作為 LLM context。

## LLM 回答生成

主要生成模型使用 IBM watsonx.ai。

必要環境變數：

```env
IBM_API_KEY=
IBM_PROJECT_ID=
IBM_CLOUD_URL=https://us-south.ml.cloud.ibm.com
IBM_MODEL_ID=openai/gpt-oss-120b
```

回答流程：

- 使用 top chunks 建立 context。
- 將使用者問題與 context 組成 prompt。
- 呼叫 IBM watsonx.ai 產生回答。
- 使用 OpenCC 轉為台灣繁體中文。
- 使用後處理移除多餘標記、引用殘留與過長輸出。

## OpenAI fallback

OpenAI fallback 在 `fallback.py`。

啟用方式：

```env
ENABLE_OPENAI_FALLBACK=true
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OAI_MIN_LOCAL_SCORE=0.32
```

當本機 RAG 的 rerank 分數低於門檻時，系統可改用 OpenAI fallback。fallback 仍會先嘗試使用本機檢索出的 context，避免回答完全脫離知識庫。

## LINE Bot 與圖片回覆

LINE 相關流程在 `rag_cli.py`。

必要環境變數：

```env
LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
```

圖片回覆使用 `static/brand/` 中的品牌與產品圖片。若要讓 LINE 使用圖片訊息，圖片 URL 需要是公開 HTTPS URL，可設定：

```env
BRAND_ASSET_BASE_URL=
BRAND_PREVIEW_BASE_URL=
PLACEHOLDER_IMAGE_URL=
```

LINE webhook 需要設定到：

```text
https://your-domain.example/callback
```

## 專案結構

```text
.
├── rag_cli.py                 # Flask、LINE webhook、查詢改寫、hybrid retrieval、RRF、rerank、LLM 回答
├── fallback.py                # OpenAI fallback
├── product_faq.py             # 產品清單、品牌別名、弱查詢補強、導引回覆
├── query_normalize.py         # 查詢正規化工具
├── ingest_xlsx.py             # 從 TXT/Excel 切塊、embedding、建立或 append FAISS index
├── build_index.py             # 從 chunks.parquet 建立 FAISS index
├── export_chunks.py           # 從 Hive 匯出 chunks.parquet
├── Additional information/    # Excel 知識來源
├── txt_docs/                  # TXT 知識來源
├── rag_index/                 # md_chunks.faiss 與 md_meta.parquet
├── static/brand/              # 品牌與產品圖片
├── requirements.txt           # Python 相依套件
└── .env.example               # 環境變數範例
```

## 安裝

需求：

- Python 3.10 或更新版本
- 可連線下載 Hugging Face 模型
- LINE Developers channel access token 與 channel secret
- IBM Cloud watsonx.ai API key 與 project ID
- OpenAI API key（選用）

安裝套件：

```bash
pip install -r requirements.txt
```

初次執行會下載 embedding model 與 reranker model，可能需要較久時間。

## 環境設定

複製 `.env.example` 成 `.env`：

```bash
cp .env.example .env
```

至少需要填：

```env
IBM_API_KEY=
IBM_PROJECT_ID=
IBM_CLOUD_URL=https://us-south.ml.cloud.ibm.com
IBM_MODEL_ID=openai/gpt-oss-120b

LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
```

若啟用 OpenAI fallback：

```env
ENABLE_OPENAI_FALLBACK=true
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

請勿提交 `.env`；repo 只保留 `.env.example`。

## 環境變數參考

| 變數 | 用途 | 預設/範例 |
| --- | --- | --- |
| `LOG_LEVEL` | log 等級 | `INFO` |
| `IBM_API_KEY` | IBM watsonx.ai API key，必填 | 空 |
| `IBM_PROJECT_ID` | IBM watsonx.ai project ID，必填 | 空 |
| `IBM_CLOUD_URL` | IBM Cloud endpoint | `https://us-south.ml.cloud.ibm.com` |
| `IBM_MODEL_ID` | watsonx.ai 生成模型 | `openai/gpt-oss-120b` |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot access token，必填 | 空 |
| `LINE_CHANNEL_SECRET` | LINE Bot channel secret，必填 | 空 |
| `PORT` | Flask server port | `8000` |
| `MAX_NEW_TOKENS` | LLM 最大生成 token | `120` |
| `MAX_OUTPUT_SENTENCES` | 回答句數上限 | `1` |
| `MAX_OUTPUT_CHARS` | 回答字數上限 | `180` |
| `RERANKER_ID` | primary reranker | `BAAI/bge-reranker-v2-m3` |
| `RERANKER_FALLBACK_ID` | fallback reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| `READ_TXT` | `ingest_xlsx.py` 是否讀 TXT | `true` |
| `READ_XLSX` | `ingest_xlsx.py` 是否讀 Excel | `false` |
| `REBUILD_MODE` | 是否重建整個索引 | `false` |
| `TXT_DIR` | TXT 知識來源資料夾 | `txt_docs` |
| `EXCEL_PATH` | Excel 知識來源路徑 | `Additional information` |
| `OUT_DIR` | 索引輸出資料夾 | `rag_index` |
| `EMBED_MODEL` | embedding 模型 | `intfloat/multilingual-e5-base` |
| `BATCH` | embedding batch size | `64` |
| `MAX_CHARS` | chunk 最大字元數 | `800` |
| `OVERLAP` | chunk 重疊字元數 | `100` |
| `BRAND_ASSET_BASE_URL` | 品牌圖片公開 base URL | 空 |
| `BRAND_PREVIEW_BASE_URL` | LINE preview 圖片 base URL | 空 |
| `PLACEHOLDER_IMAGE_URL` | 圖片 fallback URL | 空 |
| `ENABLE_OPENAI_FALLBACK` | 是否啟用 OpenAI fallback | `true` |
| `OPENAI_API_KEY` | OpenAI API key | 空 |
| `OPENAI_MODEL` | OpenAI fallback 模型 | `gpt-4.1-mini` |
| `OAI_MIN_LOCAL_SCORE` | fallback 觸發門檻 | `0.32` |
| `OPENAI_TIMEOUT_SEC` | OpenAI timeout 秒數 | `15` |
| `OPENAI_MAX_NEW_TOKENS` | OpenAI fallback 最大 token | `220` |

## 執行

啟動 webhook server：

```bash
python rag_cli.py
```

預設監聽：

```text
0.0.0.0:8000
```

指定 port：

```bash
PORT=8080 python rag_cli.py
```

若要接 LINE webhook，需要用 ngrok、Cloudflare Tunnel 或正式部署環境提供公開 HTTPS URL。

## 重建索引

從 TXT/Excel 重建或 append：

```bash
python ingest_xlsx.py
```

完整重建：

```bash
REBUILD_MODE=true python ingest_xlsx.py
```

從 `chunks.parquet` 重建：

```bash
python build_index.py
```

## 常見錯誤排除

### 缺少 IBM 金鑰

錯誤類型：

```text
RuntimeError: 請在 .env 設定 IBM_API_KEY 與 IBM_PROJECT_ID
```

處理方式：

- 確認 `.env` 存在。
- 確認 `IBM_API_KEY` 與 `IBM_PROJECT_ID` 已填入。
- 確認 IBM Cloud URL 與 project ID 對應同一個 watsonx.ai project。

### 缺少 LINE 金鑰

錯誤類型：

```text
RuntimeError: 請在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET
```

處理方式：

- 到 LINE Developers 後台確認 channel access token。
- 確認 channel secret 沒有貼錯。
- 修改 `.env` 後重新啟動 `python rag_cli.py`。

### 找不到 FAISS 或 metadata

可能原因：

- `rag_index/md_chunks.faiss` 不存在。
- `rag_index/md_meta.parquet` 不存在。
- 工作目錄不是專案根目錄。

處理方式：

```bash
python ingest_xlsx.py
```

或如果已有 `chunks.parquet`：

```bash
python build_index.py
```

### Hugging Face 模型下載很久

第一次啟動會下載：

- `intfloat/multilingual-e5-base`
- `BAAI/bge-reranker-v2-m3`
- fallback 時可能下載 `cross-encoder/ms-marco-MiniLM-L-6-v2`

處理方式：

- 確認主機可連線到 Hugging Face。
- 第一次啟動等待模型下載完成。
- 若環境不能連外，需先在可連網環境預下載模型快取。

### LINE webhook 沒收到訊息

檢查項目：

- Flask server 是否正在執行。
- LINE Developers webhook URL 是否是公開 HTTPS。
- URL 是否包含 `/callback`。
- ngrok 或 Cloudflare Tunnel 是否仍在運作。
- LINE 後台 webhook 是否啟用。
- `LINE_CHANNEL_ACCESS_TOKEN` 與 `LINE_CHANNEL_SECRET` 是否來自同一個 channel。

### LINE 圖片無法顯示

LINE image message 需要公開 HTTPS 圖片 URL。本機檔案路徑不能直接給 LINE 使用。

處理方式：

- 將圖片部署到公開靜態檔服務。
- 設定 `BRAND_ASSET_BASE_URL` 或 `BRAND_PREVIEW_BASE_URL`。
- 確認圖片網址可以從外部瀏覽器直接打開。

### OpenAI fallback 沒有作用

檢查項目：

- `ENABLE_OPENAI_FALLBACK=true`
- `OPENAI_API_KEY` 已填入。
- `OPENAI_MODEL` 正確。
- 本機 RAG 分數低於 `OAI_MIN_LOCAL_SCORE` 時才會進 fallback。

## 注意事項

- `.env` 必須填 IBM 與 LINE 金鑰，否則 `rag_cli.py` 會直接停止。
- OpenAI fallback 開啟時需要 `OPENAI_API_KEY`。
- `rag_index/md_chunks.faiss` 與 `rag_index/md_meta.parquet` 必須存在，主程式才能檢索。
- 第一次執行會下載模型，請確認網路可連 Hugging Face。
- LINE 圖片訊息需要公開 HTTPS 圖片 URL。
