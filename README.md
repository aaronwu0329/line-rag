# LINE RAG Product Assistant

LINE RAG Product Assistant is a Flask-based LINE Bot that answers product and solution questions with retrieval-augmented generation. It combines local product documents, FAISS vector search, BM25 keyword search, reranking, IBM watsonx.ai generation, and an optional OpenAI fallback for low-confidence answers.

## Features

- LINE Messaging API webhook built with Flask.
- Hybrid retrieval with FAISS vector search and BM25 keyword matching.
- Sentence-transformers embeddings and cross-encoder reranking.
- IBM watsonx.ai as the primary answer generation backend.
- Optional OpenAI fallback when local retrieval confidence is low.
- Product and brand helper flows for IBM, Palo Alto Networks, Qlik, Splunk, SUSE, Synopsys, TIBCO, MongoDB, Cloudera, CloudCasa, SAS, and related IBM Power models.
- Brand/product image responses through LINE image messages.
- Excel, TXT, Parquet, and FAISS index build scripts for maintaining the knowledge base.

## Project Structure

```text
.
├── rag_cli.py                 # Main Flask + LINE Bot application
├── fallback.py                # Optional OpenAI fallback integration
├── product_faq.py             # Product catalog, aliases, and guided replies
├── query_normalize.py         # Query normalization helpers
├── ingest_xlsx.py             # Build/update RAG index from Excel and TXT sources
├── build_index.py             # Build FAISS index from chunks.parquet
├── export_chunks.py           # Export source chunks
├── Additional information/    # Excel knowledge sources
├── txt_docs/                  # TXT knowledge sources
├── rag_index/                 # Generated FAISS index and metadata
└── static/brand/              # Brand and product images
```

## Requirements

- Python 3.10+
- LINE Developers channel access token and channel secret
- IBM Cloud watsonx.ai API key and project ID
- Optional OpenAI API key for fallback answers

Install dependencies:

```bash
pip install -r requirements.txt
```

Some packages, especially `faiss-cpu`, `sentence-transformers`, and model downloads, can take time to install on a fresh machine.

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:

```env
IBM_API_KEY=
IBM_PROJECT_ID=
IBM_CLOUD_URL=https://us-south.ml.cloud.ibm.com
IBM_MODEL_ID=meta-llama/llama-4-maverick-17b-128e-instruct-fp8

LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
```

Optional fallback variables:

```env
ENABLE_OPENAI_FALLBACK=true
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

Do not commit `.env`; only `.env.example` should be shared.

## Run

Start the webhook server:

```bash
python rag_cli.py
```

The app listens on `0.0.0.0:8000` by default. Set `PORT` to change it:

```bash
PORT=8080 python rag_cli.py
```

Expose the server through a public HTTPS URL, then configure the LINE webhook URL:

```text
https://your-domain.example/callback
```

## Rebuild the Knowledge Index

To rebuild or update the RAG index from the Excel and TXT sources:

```bash
python ingest_xlsx.py
```

To rebuild from `chunks.parquet`:

```bash
python build_index.py
```

Generated files are stored under `rag_index/`.

## Notes

- Keep API keys, channel secrets, and private credentials in `.env`.
- The repository includes sample/source knowledge files and a prebuilt local index so the bot can be restored more easily.
- Large model files are downloaded by `sentence-transformers` at runtime and are not stored in this repository.

