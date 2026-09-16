"""SQLite 缓存：保存商品的 clusterID、名称、缩略图，供下次启动复用。

watchlist.txt 是唯一的跟踪标准——每次同步都以它为准：
清单里有、缓存里没有的补上；缓存里有、清单里没有的删掉。

缓存只是加速用的，丢了还能重新抓：所以这里的原则是"能起来就行"。
数据库文件损坏、目录不存在、条目形状不对，都在模块内降级处理
（重建空库 / 自己建目录 / 跳过该条）并记进 notes，绝不抛给调用方。
"""

import os
import sqlite3
from datetime import datetime

DB_FILENAME = "cache.db"


class Store:
    def __init__(self, path: str):
        self.path = path
        self.notes = []  # 本次操作的异常情况，供调用方取用（每次公开操作会清空）
        self.conn = self._open_or_recover(path)

    # ---------------- 打开与自愈 ----------------

    def _open_or_recover(self, path):
        try:
            return self._connect(path)
        except sqlite3.DatabaseError as err:
            return self._rebuild(path, f"缓存库损坏（{err}）")
        except OSError as err:
            self._note(f"缓存库不可用（{err}），本次运行改用内存缓存")
            return self._connect(":memory:")

    def _connect(self, path):
        if path != ":memory:":
            # 目录被删掉（清理工具、手动删 data/）时自己建出来
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            self._init_db(conn)
        except BaseException:
            conn.close()  # 建表失败就别留着这个半开的连接
            raise
        return conn

    def _rebuild(self, path, reason):
        """把坏掉的库改名备份后重建空库；备份也做不了就退化成内存库。"""
        backup = _backup_path(path)
        try:
            if os.path.exists(path):
                os.replace(path, backup)  # 改名而不是删除，坏库留着排查
            for suffix in ("-wal", "-shm", "-journal"):  # 边角文件一起挪走
                if os.path.exists(path + suffix):
                    os.replace(path + suffix, backup + suffix)
        except OSError as err:
            self._note(f"{reason}，且备份失败（{err}），本次运行改用内存缓存")
            return self._connect(":memory:")

        self._note(f"{reason}，已备份为 {os.path.basename(backup)} 并重建空缓存")
        try:
            return self._connect(path)
        except (sqlite3.DatabaseError, OSError) as err:
            self._note(f"重建缓存失败（{err}），本次运行改用内存缓存")
            return self._connect(":memory:")

    def _init_db(self, conn):
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS items (
                       cluster_id TEXT PRIMARY KEY,
                       name       TEXT,
                       image_url  TEXT,
                       updated_at TEXT
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                       key   TEXT PRIMARY KEY,
                       value TEXT
                   )"""
            )

    def _begin(self):
        """每个公开操作的第一句：清掉上次的提示，并确认连接还在。"""
        self.notes = []
        if self.conn is None:
            raise RuntimeError("缓存已关闭，无法继续操作")

    def _note(self, message: str):
        self.notes.append(message)

    def close(self):
        """关闭连接（可重复调用）。"""
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # ---------------- 商品缓存 ----------------

    def get_item(self, cluster_id: str):
        """按 clusterID 查缓存，没有返回 None。"""
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM items WHERE cluster_id = ?", (cluster_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all(self):
        """返回全部缓存记录（新抓取的在前）。"""
        self._begin()
        rows = self.conn.execute(
            "SELECT * FROM items ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def sync(self, items):
        """让缓存严格跟随 watchlist，返回 (新增数, 删除数)。

        items 为 [(clusterId, 商品名)]；清单里的名字只用于补空，
        已经抓取到的名字不会被清单里的旧名字覆盖。
        形状不对或 ID 非法的条目跳过（记进 notes），不打断整轮同步——
        一条坏记录不该让整个清单同步不上。
        """
        self._begin()
        now = _now()
        wanted = {}
        skipped = 0
        for item in items or []:
            cluster_id, name = _split_item(item)
            if cluster_id is None:
                skipped += 1
                continue
            wanted[cluster_id] = _clean_name(name)
        if skipped:
            self._note(f"清单里有 {skipped} 条记录无法识别，已跳过")

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
        """保存抓取结果；新值为空时保留旧值。

        空串按"没抓到"处理：SQLite 里空串不是 NULL，COALESCE 挡不住它，
        会把已经缓存好的名字/缩略图冲掉。
        """
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            self._note("抓取结果里的 clusterId 不合法，已跳过缓存写入")
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO items (cluster_id, name, image_url, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                       name       = COALESCE(excluded.name, name),
                       image_url  = COALESCE(excluded.image_url, image_url),
                       updated_at = excluded.updated_at""",
                (
                    cluster_id,
                    _clean_name(name),
                    _clean_cached_text(image_url),
                    _now(),
                ),
            )

    def delete_item(self, cluster_id: str):
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            return
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE cluster_id = ?", (cluster_id,))

    # ---------------- 设置 ----------------

    def get_setting(self, key: str, default=None):
        self._begin()
        if not isinstance(key, str):
            return default
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self._begin()
        if not isinstance(key, str) or value is None:
            self._note("设置的键值不合法，已跳过写入")
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, str(value)),
            )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _backup_path(path: str) -> str:
    """坏库的备份名：cache.db -> cache.db.bad-20260916T101530（按时间戳区分）。"""
    return f"{path}.bad-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def _clean_id(cluster_id):
    """clusterId 只认纯数字字符串（int 也接受）；其余返回 None。"""
    if cluster_id is None or isinstance(cluster_id, bool):
        return None
    text = str(cluster_id).strip()
    return text if text.isdigit() else None


def _split_item(item):
    """拆开 (clusterId, 名称) 并校验 ID；形状不对返回 (None, "")。"""
    try:
        cluster_id, name = item
    except (TypeError, ValueError):
        return None, ""
    return _clean_id(cluster_id), name


def _clean_name(name):
    """名字归一：非字符串或空白按"没有"处理（None 才会走 COALESCE 保留旧值）。"""
    if not isinstance(name, str):
        return None
    text = " ".join(name.split())
    return text or None


def _clean_cached_text(value):
    """缓存里的文本字段归一：非字符串或空白按"没有"处理，其余原样存。

    这里不校验 URL 是否合法——缓存只是拿来加速显示的，存进去个怪链接
    最多是缩略图加载不出来，不值得为它丢掉一个将来可能还有用的值。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def default_db_path() -> str:
    """缓存数据库路径（项目根目录下的 data/ 目录，不存在则创建）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, DB_FILENAME)
