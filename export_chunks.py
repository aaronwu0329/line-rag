import pandas as pd
from pyhive import hive

HIVE_HOST = "cdp01"      #  HMS/HS2 host
HIVE_PORT = 10000        # 預設 10000
DB, TBL = "default", "markdown_chunks"   # 生成的表

sql = f"""
SELECT vendor, doc_name, chunk_id, title, text
FROM {DB}.{TBL}
ORDER BY vendor, doc_name, chunk_id
"""

print("Connecting to Hive...")
conn = hive.Connection(host=HIVE_HOST, port=HIVE_PORT, database=DB, username="aaron")
print("Querying...")
df = pd.read_sql(sql, conn)
conn.close()

print(df.head())
out = "chunks.parquet"
df.to_parquet(out, index=False)
print("✅ wrote", out, "rows:", len(df))
