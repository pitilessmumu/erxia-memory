# -*- coding: utf-8 -*-
"""探一下本机 SQLite 的家底：版本、FTS5 有没有、能用哪些分词器。"""
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

print("sqlite", sqlite3.sqlite_version)
db = sqlite3.connect(":memory:")
for tok in ("unicode61", "trigram", "porter", "ascii"):
    try:
        db.execute("CREATE VIRTUAL TABLE t_%s USING fts5(x, tokenize='%s')" % (tok, tok))
        print("分词器", tok, "可用")
    except Exception as e:
        print("分词器", tok, "不可用：", e)
try:
    db.execute("CREATE VIRTUAL TABLE b USING fts5(x)")
    db.executemany("INSERT INTO b(x) VALUES (?)", [("钩子收不到",), ("换壳之后记忆全忘了",)])
    rows = db.execute("SELECT x, bm25(b) FROM b WHERE b MATCH ? ORDER BY bm25(b)", ("钩子 OR 收不",)).fetchall()
    print("bm25 查询通了：", rows)
except Exception as e:
    print("bm25 查询失败：", e)
