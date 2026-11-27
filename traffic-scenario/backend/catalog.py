import hashlib
import json
import random
import sqlite3
from contextlib import contextmanager
from pathlib import Path

CATEGORIES = ["cookie", "gift", "ornament", "hot_choco", "toy"]
NAMES = {"cookie": "쿠키", "gift": "선물", "ornament": "장식", "hot_choco": "핫초코", "toy": "장난감"}
QUERY = """SELECT product_id,name,price,shop_name,location FROM products
WHERE category=? AND price<=? ORDER BY price,product_id LIMIT 10"""
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_products_category_price_id ON products(category,price,product_id)"


# SQLite DB 연결
@contextmanager
def connect(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row

    try:
        with con:
            yield con
    finally:
        con.close()


# 테스트용 상품 데이터 생성
def initialize(path: Path, count=100000, seed=42):
    path.parent.mkdir(parents=True, exist_ok=True)

    with connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")

        if con.execute("SELECT 1 FROM metadata WHERE key='dataset_hash'").fetchone():
            return

        con.execute("CREATE TABLE products(product_id INTEGER PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,price INTEGER NOT NULL,shop_name TEXT NOT NULL,location TEXT NOT NULL)")
        rng, h = random.Random(seed), hashlib.sha256()
        batch = []

        for i in range(1, count + 1):
            c = CATEGORIES[rng.randrange(len(CATEGORIES))]
            row = (i, f"크리스마스 {NAMES[c]} {i}", c, rng.randrange(1000, 100001, 100), f"{NAMES[c]} 가게 {i % 25 + 1}", f"광장 {i % 8 + 1}구역")
            h.update((json.dumps(row, ensure_ascii=False) + "\n").encode())
            batch.append(row)

            if len(batch) == 1000:
                con.executemany("INSERT INTO products VALUES(?,?,?,?,?,?)", batch)
                batch.clear()

        if batch:
            con.executemany("INSERT INTO products VALUES(?,?,?,?,?,?)", batch)

        con.executemany("INSERT INTO metadata VALUES(?,?)", [("dataset_hash", "sha256:" + h.hexdigest()), ("count", str(count)), ("seed", str(seed))])


# 조건에 맞는 상품 DB 조회
def search(path, category, max_price):
    with connect(path) as con:
        return [dict(r) for r in con.execute(QUERY, (category, max_price))]


# 현재 DB 스키마 조회
def schema(path):
    with connect(path) as con:
        return "\n".join(r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))


# 상품 데이터 변경 여부 확인
def dataset_hash(path):
    # 실제 상품 데이터를 읽어 데이터 변경 여부 확인
    h = hashlib.sha256()

    with connect(path) as con:
        for row in con.execute("SELECT product_id,name,category,price,shop_name,location FROM products ORDER BY product_id"):
            h.update((json.dumps(tuple(row), ensure_ascii=False) + "\n").encode())

    return "sha256:" + h.hexdigest()


# 검색 쿼리 실행 계획 조회
def plan(path, category="cookie", max_price=20000):
    with connect(path) as con:
        return [dict(r) for r in con.execute("EXPLAIN QUERY PLAN " + QUERY, (category, max_price))]
