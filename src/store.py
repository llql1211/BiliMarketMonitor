"""SQLite 缓存：保存已抓取商品的 clusterID、名称、缩略图，供下次启动复用。

watchlist.txt 仅作为输入；第一次抓取后把商品信息存入本库，
之后启动时命中缓存的商品只需抓取价格信息，其余复用。
"""

import os
import sqlite3
from datetime import datetime

DB_FILENAME = "cache.db"


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS items (
                       cluster_id TEXT PRIMARY KEY,
                       name       TEXT,
                       image_url  TEXT,
                       updated_at TEXT
                   )"""
            )

    def get_item(self, cluster_id: str):
        """按 clusterID 查缓存，没有返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM items WHERE cluster_id = ?", (cluster_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all(self):
        """返回全部缓存记录（新抓取的在前）。"""
        rows = self.conn.execute(
            "SELECT * FROM items ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_item(self, cluster_id: str, name, image_url):
        """插入或更新一条缓存；新值为空时保留旧值。"""
        with self.conn:
            self.conn.execute(
                """INSERT INTO items (cluster_id, name, image_url, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                       name       = COALESCE(excluded.name, name),
                       image_url  = COALESCE(excluded.image_url, image_url),
                       updated_at = excluded.updated_at""",
                (cluster_id, name, image_url,
                 datetime.now().isoformat(timespec="seconds")),
            )


def default_db_path() -> str:
    """缓存数据库路径（项目根目录，即 src/ 的上一级）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, DB_FILENAME)
