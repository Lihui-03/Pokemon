import sqlite3
import threading
from pathlib import Path

from catalog import SEED_PRODUCTS

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "watches.db"


class Database:
    def __init__(self, path: Path = DB_PATH):
        DATA.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    retailer TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_status TEXT,
                    last_detail TEXT,
                    last_price TEXT,
                    last_checked TEXT,
                    UNIQUE(retailer, sku)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchase_attempts (
                    id INTEGER PRIMARY KEY,
                    sku TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    order_number TEXT,
                    total TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def seed(self) -> int:
        added = 0
        for item in SEED_PRODUCTS:
            if self.add_product(**item):
                added += 1
        return added

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def add_product(self, retailer: str, sku: str, name: str, url: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO products(retailer, sku, name, url)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(retailer, sku) DO NOTHING
                """,
                (retailer, sku, name, url),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def products(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM products"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY retailer, name"
        with self._lock:
            return list(self._conn.execute(sql).fetchall())

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()

    def set_enabled(self, product_id: int, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE products SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, product_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_product(self, product_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM products WHERE id = ?", (product_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def save_check(
        self,
        product_id: int,
        *,
        status: str | None,
        detail: str,
        price: str | None,
        name: str | None,
        checked_at: str,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, last_price FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                """
                UPDATE products
                SET last_status = COALESCE(?, last_status),
                    last_detail = ?,
                    last_price = COALESCE(?, last_price),
                    last_checked = ?,
                    name = COALESCE(?, name)
                WHERE id = ?
                """,
                (
                    status,
                    detail,
                    price if price else row["last_price"],
                    checked_at,
                    name or row["name"],
                    product_id,
                ),
            )
            self._conn.commit()

    def record_purchase(
        self,
        *,
        sku: str,
        status: str,
        detail: str,
        order_number: str | None,
        total: str | None,
        created_at: str,
    ) -> None:
        detail = detail[:500]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO purchase_attempts(sku, status, detail, order_number, total, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (sku, status, detail, order_number, total, created_at),
            )
            self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES('purchase_status', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (status,),
            )
            self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES('purchase_detail', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (detail,),
            )
            if order_number:
                self._conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES('purchase_order_number', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (order_number,),
                )
            if total:
                self._conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES('purchase_total', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (total,),
                )
            self._conn.commit()

    def latest_purchase(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM purchase_attempts ORDER BY id DESC LIMIT 1"
            ).fetchone()
