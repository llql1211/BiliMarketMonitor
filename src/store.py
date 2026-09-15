"""SQLite 缓存：保存商品的 clusterID、名称、缩略图，供下次启动复用。

watchlist.txt 是唯一的跟踪标准——每次同步都以它为准：
清单里有、缓存里没有的补上；缓存里有、清单里没有的删掉。
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
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                       key   TEXT PRIMARY KEY,
                       value TEXT
                   )"""
            )

    # ---------------- 商品缓存 ----------------

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

    def sync(self, items):
        """让缓存严格跟随 watchlist，返回 (新增数, 删除数)。

        items 为 [(clusterId, 商品名)]；清单里的名字只用于补空，
        已经抓取到的名字不会被清单里的旧名字覆盖。
        """
        now = _now()
        wanted = {cid: (name or None) for cid, name in items}
        added = removed = 0

        with self.conn:
            for cluster_id, name in wanted.items():
                exists = self.conn.execute(
                    "SELECT 1 FROM items WHERE cluster_id = ?", (cluster_id,)
                ).fetchone()
                if exists:
                    if name:
                        self.conn.execute(
                            """UPDATE items SET name = ?
                               WHERE cluster_id = ? AND (name IS NULL OR name = '')""",
                            (name, cluster_id),
                        )
                else:
                    self.conn.execute(
                        """INSERT INTO items (cluster_id, name, image_url, updated_at)
                           VALUES (?, ?, NULL, ?)""",
                        (cluster_id, name, now),
                    )
                    added += 1

            if wanted:
                placeholders = ",".join("?" * len(wanted))
                cur = self.conn.execute(
                    f"DELETE FROM items WHERE cluster_id NOT IN ({placeholders})",
                    tuple(wanted),
                )
            else:
                cur = self.conn.execute("DELETE FROM items")
            removed = cur.rowcount

        return added, removed

    def upsert_item(self, cluster_id: str, name, image_url):
        """保存抓取结果；新值为空时保留旧值。"""
        with self.conn:
            self.conn.execute(
                """INSERT INTO items (cluster_id, name, image_url, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                       name       = COALESCE(excluded.name, name),
                       image_url  = COALESCE(excluded.image_url, image_url),
                       updated_at = excluded.updated_at""",
                (cluster_id, name, image_url, _now()),
            )

    def delete_item(self, cluster_id: str):
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE cluster_id = ?", (cluster_id,))

    # ---------------- 设置 ----------------

    def get_setting(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self.conn:
            self.conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_db_path() -> str:
    """缓存数据库路径（项目根目录下的 data/ 目录，不存在则创建）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, DB_FILENAME)
