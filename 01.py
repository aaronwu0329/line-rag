from pyhive import hive
import pandas as pd

pd.set_option("display.unicode.east_asian_width", True)
pd.set_option("display.unicode.ambiguous_as_wide", True)
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

DO_INSERT = False

INSERT_DT = "2025-08-22"
INSERT_STOCK = "朋昶"
INSERT_PRICE = "1234.5"

insert_sql = f"""
INSERT INTO stock_typed (dt, stock_name, close_price)
VALUES ('{INSERT_DT}', '{INSERT_STOCK}', {INSERT_PRICE})
"""

delete_sql = f"""
DELETE FROM stock_typed
WHERE dt='{INSERT_DT}' AND stock_name='{INSERT_STOCK}' AND close_price={INSERT_PRICE}
"""

top10_sql = """
WITH stats AS (
  SELECT dt, AVG(close_price) AS avg_p, STDDEV_SAMP(close_price) AS sd_p
  FROM stock_typed
  WHERE close_price IS NOT NULL
  GROUP BY dt
),
z AS (
  SELECT s.dt, t.stock_name, t.close_price,
         CASE WHEN s.sd_p = 0 OR s.sd_p IS NULL THEN NULL
              ELSE (t.close_price - s.avg_p) / s.sd_p END AS zscore
  FROM stock_typed t JOIN stats s ON t.dt = s.dt
  WHERE t.close_price IS NOT NULL
),
ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY dt ORDER BY ABS(zscore) DESC) AS rn
  FROM z
)
SELECT dt AS `日期`,
       stock_name AS `股票名稱`,
       close_price AS `收盤價`,
       ROUND(zscore,2) AS `z分數`
FROM ranked
WHERE rn <= 10
ORDER BY ABS(`z分數`) DESC
"""


CONN_KW = dict(
    host="cdp01",
    port=10000,
    database="default",
    auth="NONE",
    username="hive",
    password=None,
   
    configuration={
        "hive.query.name": "python_stock_top10",
        "hive.execution.engine": "tez",
        "tez.queue.name": "default",
        "tez.am.resource.memory.mb": "512",
        "tez.task.resource.memory.mb": "512",
        "hive.tez.container.size": "512",
        "hive.vectorized.execution.enabled": "true",
        "hive.exec.reducers.max": "8",
    },
)


with hive.Connection(**CONN_KW) as conn:
    with conn.cursor() as cur:
        if DO_INSERT:
            cur.execute(insert_sql)
        else:
            try:
                cur.execute(delete_sql)
            except Exception as e:
                print("刪除失敗:", e)

    df = pd.read_sql(top10_sql, conn)

print(df)
