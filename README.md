# LINE RAG Product Assistant

這是一個以 Flask 建立的 LINE Bot 產品問答助理，使用 RAG（Retrieval-Augmented Generation）流程回答產品、方案與技術資訊問題。系統會結合本機知識文件、FAISS 向量搜尋、BM25 關鍵字搜尋、reranking、IBM watsonx.ai 文字生成，並可在低信心回答時啟用 OpenAI fallback。

## 功能特色

- 使用 Flask 提供 LINE Messaging API webhook。
- 以 FAISS 向量搜尋搭配 BM25 關鍵字搜尋做混合檢索。
- 使用 sentence-transformers 產生 embeddings，並以 cross-encoder 重新排序候選內容。
- 以 IBM watsonx.ai 作為主要回答生成後端。
- 可選擇啟用 OpenAI fallback，處理本機檢索信心不足的問題。
- 內建產品與品牌導引回覆，涵蓋 IBM、Palo Alto Networks、Qlik、Splunk、SUSE、Synopsys、TIBCO、MongoDB、Cloudera、CloudCasa、SAS 與 IBM Power 系列型號。
- 支援透過 LINE image message 回傳品牌或產品圖片。
- 提供 Excel、TXT、Parquet 與 FAISS index 建置腳本，方便維護知識庫。

## 專案結構

```text
.
├── rag_cli.py                 # Flask 與 LINE Bot 主程式
├── fallback.py                # OpenAI fallback 整合
├── product_faq.py             # 產品清單、別名與導引回覆
├── query_normalize.py         # 查詢正規化工具
├── ingest_xlsx.py             # 從 Excel/TXT 建立或更新 RAG index
├── build_index.py             # 從 chunks.parquet 建立 FAISS index
├── export_chunks.py           # 匯出文字區塊
├── Additional information/    # Excel 知識來源
├── txt_docs/                  # TXT 知識來源
├── rag_index/                 # 已建立的 FAISS index 與 metadata
├── static/brand/              # 品牌與產品圖片
├── requirements.txt           # Python 相依套件
└── .env.example               # 環境變數範例
```

## 系統需求

- Python 3.10 或更新版本
- LINE Developers channel access token 與 channel secret
- IBM Cloud watsonx.ai API key 與 project ID
- OpenAI API key（選用，只有啟用 fallback 時需要）

安裝相依套件：

```bash
pip install -r requirements.txt
```

初次安裝時，`faiss-cpu`、`sentence-transformers` 與模型下載可能需要較久時間。

## 環境設定

複製 `.env.example` 成 `.env`，再填入實際金鑰與設定值：

```bash
cp .env.example .env
```

必要變數：

```env
IBM_API_KEY=
IBM_PROJECT_ID=
IBM_CLOUD_URL=https://us-south.ml.cloud.ibm.com
IBM_MODEL_ID=meta-llama/llama-4-maverick-17b-128e-instruct-fp8

LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
```

OpenAI fallback 選用變數：

```env
ENABLE_OPENAI_FALLBACK=true
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

請勿提交 `.env`；專案只應分享 `.env.example`。

## 執行方式

啟動 webhook server：

```bash
python rag_cli.py
```

預設會監聽 `0.0.0.0:8000`。如需指定 port，可設定 `PORT`：

```bash
PORT=8080 python rag_cli.py
```

LINE webhook 需要公開 HTTPS URL。將本機服務透過 ngrok、Cloudflare Tunnel 或正式部署環境公開後，在 LINE Developers 後台設定：

```text
https://your-domain.example/callback
```

## 重建知識索引

從 Excel 與 TXT 來源重建或更新 RAG index：

```bash
python ingest_xlsx.py
```

從 `chunks.parquet` 重建 FAISS index：

```bash
python build_index.py
```

產生的索引檔會儲存在 `rag_index/`。

## 注意事項

- API key、LINE channel secret 與其他私密設定請放在 `.env`。
- 此 repo 包含知識來源檔案與預先建立的本機 index，方便還原與部署。
- sentence-transformers 需要的模型會在執行時下載，不會存放在此 repository。
