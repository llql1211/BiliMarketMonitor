"""SQLite 缓存：保存商品的 clusterID、名称、缩略图、上次抓到的价格和成交。

watchlist.txt 是唯一的跟踪标准——每次同步都以它为准：
清单里有、缓存里没有的补上；缓存里有、清单里没有的删掉。

缓存只是加速用的，丢了还能重新抓：所以这里的原则是"能起来就行"。
数据库文件损坏、目录不存在、条目形状不对，都在模块内降级处理
（重建空库 / 自己建目录 / 跳过该条）并记进 notes，绝不抛给调用方。
"""

import json
import os
import sqlite3
from datetime import datetime

DB_FILENAME = "cache.db"

# items 表的列。建表和"给老库补列"都从这里生成，免得两处写岔、
# 出现"新建的库有这列、老库没有"这种只在别人机器上炸的毛病。
# 加字段就往这里加，不用管老库——_ensure_item_columns 会补上。
ITEM_COLUMNS = (
    ("cluster_id", "TEXT PRIMARY KEY"),
    ("name", "TEXT"),
    ("favorite", "INTEGER DEFAULT 0"),  # 用户手动打的收藏标记（0/1）。
                                        # 跟名字的来路不同：抓取碰不到它（见
                                        # upsert_item 的列清单），只有点星和清单
                                        # 同步两处会写
    ("image_url", "TEXT"),
    ("updated_at", "TEXT"),         # 名称 / 缩略图的抓取时间
    ("price_text", "TEXT"),         # 上次抓到的现价原文（如「¥44」）
    ("reference_price", "TEXT"),    # 上次抓到的原价（划线价）
    ("avg_text", "TEXT"),           # 上次抓到的近 30 天均价
    ("sold_out", "INTEGER"),        # 上次抓到的是不是售罄（0/1）
    ("stock_count", "INTEGER"),     # 上次抓到的「当前价格还剩几件」（没报就是 NULL）
    ("price_updated_at", "TEXT"),   # 上面几个价格字段是什么时候抓的
    ("lowest_price", "TEXT"),       # 历次抓到的现价里最低的那个（售罄那次不算）。
                                    # 跟上面那组快照的规则不同：只降不升，涨回去也
                                    # 不改高；是缓存里唯一一条"重抓补不回来"的记录
    ("deals_json", "TEXT"),         # 上次抓到的近 N 条成交（JSON 数组）
    ("deals_updated_at", "TEXT"),   # 上面那组成交是什么时候抓的
    # 上次「确认」时这行是什么样子（见 acknowledge / init_ack_if_empty，界面上那块
    # 「自上次确认以来」拿它当基准）。跟 favorite 一样是**用户意图**：抓取一个字都
    # 不碰——upsert_item 的列清单里没有这几列，只有确认那两处会写。
    # 它一直是"跨越几轮攒变化"的那根基线，所以只写不读的老库补列对它毫无影响：
    # 补出来是空的，就等于"这件还没确认过"，累积从下一次确认重新起算
    ("ack_price_text", "TEXT"),     # 确认时的现价原文
    ("ack_price_value", "REAL"),    # 上面那个的数值（app 算好传进来，
                                    # 保持 store 不 import parser 的单向依赖）
    ("ack_sold_out", "INTEGER"),    # 确认时是不是售罄（0/1）
    ("ack_lowest_price", "TEXT"),   # 确认时的史低价（留个"确认时贵不贵"的底）
    ("ack_deals_json", "TEXT"),     # 确认时的成交快照（跟 deals_json 同格式）
    ("ack_deals_updated_at", "TEXT"),  # 那份成交快照是什么时候抓的。**不是** ack_at：
                                       # 用户可能过很久才点确认，拿确认时刻当锚点会把
                                       # 老成交算年轻，下一轮又报成"新增"（见 app.ack_new_deals）
    ("ack_at", "TEXT"),             # 这行的基线是什么时候记下的。空 = 还没确认过
)

# settings 表里写一行的 SQL。set_setting 和 acknowledge 都要用：确认得把"确认时刻"
# 跟那一组基线放进**同一个事务**，不能借道 set_setting——它自带一个 `with self.conn:`，
# 嵌在外层事务里会先把外层提交掉，正好破坏了"要么全成、要么全不动"。
_SETTING_SQL = """INSERT INTO settings (key, value) VALUES (?, ?)
                  ON CONFLICT(key) DO UPDATE SET value = excluded.value"""

# 「上次确认是什么时候」记在 settings 里（值见 acknowledge）。它是全局一个时刻，
# 不是按商品记的：确认的语义就是"这一屏我都看过了"
ACK_AT_SETTING = "last_ack_at"


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
                "CREATE TABLE IF NOT EXISTS items ("
                + ", ".join(f"{name} {decl}" for name, decl in ITEM_COLUMNS)
                + ")"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                       key   TEXT PRIMARY KEY,
                       value TEXT
                   )"""
            )
            self._ensure_item_columns(conn)

    def _ensure_item_columns(self, conn):
        """给老库补上后来新增的列。

        CREATE TABLE IF NOT EXISTS 不会改已有的表：老缓存库缺列时，后面每次
        写入都会整条失败（no such column），而这是"能起来就行"的缓存库最不该
        出的岔子。补出来的列是空的，效果跟删掉缓存重建一样，省得用户去删库。
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        for name, decl in ITEM_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")

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

    def cached_deals(self, cluster_id: str):
        """上次抓到的成交记录，返回 (成交列表, 抓取时刻)；没有基准时是 ([], None)。

        抓取时刻是判定「这轮有没有新成交」的锚点：只有它跟这组成交是同一批抓回来的，
        算出来的时间差才作数（见 app.new_deals）。也用来把缓存那组成交重算成
        "现在多久以前"再显示（见 app._reaged_deals）。

        这条路上任何不对的地方都降级成「没有基准」——JSON 坏了、形状不是列表、
        老库还没补出这两列，都只该让这一件商品少一次新增成交的提示，不该让整轮
        抓取崩在这儿。缓存库的原则就是「能起来就行」。
        """
        return cached_deals_of(self.get_item(cluster_id))

    def get_all(self):
        """返回全部缓存记录（新抓取的在前）。"""
        self._begin()
        rows = self.conn.execute(
            "SELECT * FROM items ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def sync(self, items):
        """让缓存严格跟随 watchlist，返回 (新增数, 删除数)。

        items 为 [(clusterId, 商品名[, 收藏])]；清单里的名字只用于补空，
        已经抓取到的名字不会被清单里的旧名字覆盖。

        收藏跟名字的规则不一样：它是**以清单为准整列覆盖**的。清单是用户意图的
        家，缓存只是备份（删掉库也还能从清单恢复），所以清单说没收藏就得跟着清掉。
        两元组的老写法继续能用，代价是它等于说"这件没收藏"——调用方要保留收藏
        就得把第三段带上（app 那边两处都是带上的）。

        形状不对或 ID 非法的条目跳过（记进 notes），不打断整轮同步——
        一条坏记录不该让整个清单同步不上。
        """
        self._begin()
        now = _now()
        wanted = {}
        skipped = 0
        for item in items or []:
            cluster_id, name, favorite = _split_item(item)
            if cluster_id is None:
                skipped += 1
                continue
            wanted[cluster_id] = (_clean_name(name), favorite)
        if skipped:
            self._note(f"清单里有 {skipped} 条记录无法识别，已跳过")

        added = removed = 0
        with self.conn:
            for cluster_id, (name, favorite) in wanted.items():
                exists = self.conn.execute(
                    "SELECT 1 FROM items WHERE cluster_id = ?", (cluster_id,)
                ).fetchone()
                if exists:
                    self.conn.execute(
                        "UPDATE items SET favorite = ? WHERE cluster_id = ?",
                        (int(favorite), cluster_id),
                    )
                    if name:
                        self.conn.execute(
                            """UPDATE items SET name = ?
                               WHERE cluster_id = ? AND (name IS NULL OR name = '')""",
                            (name, cluster_id),
                        )
                else:
                    self.conn.execute(
                        """INSERT INTO items (cluster_id, name, image_url, updated_at,
                                              favorite)
                           VALUES (?, ?, NULL, ?, ?)""",
                        (cluster_id, name, now, int(favorite)),
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

    def upsert_item(
        self,
        cluster_id: str,
        name,
        image_url,
        price_text=None,
        reference_price=None,
        avg_text=None,
        sold_out=False,
        stock_count=None,
        lowest_price=None,
        deals=None,
    ):
        """保存抓取结果；新值为空时保留旧值。

        收藏（favorite）不在这里：它是用户设置，抓取结果里没有这一项，
        所以下面那份列清单里压根不出现它——新插入的行按默认值 0 走，已有的行
        一个字都不动（见 set_favorite 与 sync）。

        空串按"没抓到"处理：SQLite 里空串不是 NULL，COALESCE 挡不住它，
        会把已经缓存好的名字/缩略图冲掉。

        价格那几列是一份快照，一起写、一起留：price_text 为 None 表示这次没抓到
        价格，整组（含时间戳、售罄标记和剩余件数）原样保留，免得出现"价格是上次的、
        时间却写着刚刚"这种对不上的缓存。反之 price_text 有值时整组都按这次的结果
        写，没抓到的字段就存空——上次的参考价跟这次的新现价摆在一起只会算错折扣。

        成交（deals）是另一份快照，判据也另算：它跟着 deals_updated_at 走而不是
        price_updated_at。两者会错开——售罄的商品可能一直抓不到价格、成交却每次
        都在变——而那个时刻是算「隔了多久」的锚点，用错一个就会把老成交算成新的。
        deals=None 表示这次没抓到成交（整组保留），空列表表示抓到了、确实一条都没有，
        照实覆盖。

        史低价（lowest_price）的规则跟上面两组都不同：它是一条**只降不升**的记录，
        所以不是"这次抓到什么就存什么"，而是由调用方比过一遍再决定递不递进来
        （见 app.updated_lowest）——递进来的只该是比现存更低的那个，None 表示这次
        没有刷新它（没抓到价格、售罄、认不出数字都走这条）。这里不做比较，只按
        COALESCE 存：值给什么写什么，给 None 就保留旧值。

        返回这次写入用的时刻（成交快照的抓取时刻也是它）。调用方拿它当首次观察
        基线的锚点，省得再回读一遍（见 app.OnResultReady）。
        """
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            self._note("抓取结果里的 clusterId 不合法，已跳过缓存写入")
            return None
        now = _now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO items (cluster_id, name, image_url, updated_at,
                                      price_text, reference_price, avg_text,
                                      sold_out, stock_count, price_updated_at,
                                      lowest_price, deals_json, deals_updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                       name       = COALESCE(excluded.name, name),
                       image_url  = COALESCE(excluded.image_url, image_url),
                       updated_at = excluded.updated_at,
                       price_text = COALESCE(excluded.price_text, price_text),
                       reference_price  = CASE WHEN excluded.price_text IS NULL
                                               THEN reference_price
                                               ELSE excluded.reference_price END,
                       avg_text         = CASE WHEN excluded.price_text IS NULL
                                               THEN avg_text
                                               ELSE excluded.avg_text END,
                       sold_out         = CASE WHEN excluded.price_text IS NULL
                                               THEN sold_out
                                               ELSE excluded.sold_out END,
                       stock_count      = CASE WHEN excluded.price_text IS NULL
                                               THEN stock_count
                                               ELSE excluded.stock_count END,
                       price_updated_at = CASE WHEN excluded.price_text IS NULL
                                               THEN price_updated_at
                                               ELSE excluded.price_updated_at END,
                       lowest_price     = COALESCE(excluded.lowest_price, lowest_price),
                       deals_json       = COALESCE(excluded.deals_json, deals_json),
                       deals_updated_at = CASE WHEN excluded.deals_json IS NULL
                                               THEN deals_updated_at
                                               ELSE excluded.deals_updated_at END""",
                (
                    cluster_id,
                    _clean_name(name),
                    _clean_cached_text(image_url),
                    now,
                    _clean_cached_text(price_text),
                    _clean_cached_text(reference_price),
                    _clean_cached_text(avg_text),
                    int(bool(sold_out)),
                    _clean_count(stock_count),
                    now,
                    _clean_cached_text(lowest_price),
                    _deals_json(deals),
                    now,
                ),
            )
        return now

    def acknowledge(self, entries, at):
        """把一组商品的确认基线刷成给的值，并把全局确认时刻记进 settings。

        entries 为 [(cluster_id, price_text, price_value, sold_out, lowest_price,
        deals_json, deals_updated_at)]，一个事务写完——写一半会留下"这行的基线新、
        那行的基线旧"，之后的累积报告就开始胡说。

        deals_json 沿用 _deals_json 的格式（app 直接把缓存里那格原文递进来），
        省得两处各写一套序列化。deals_updated_at 是**这组成交抓回来的时刻**，
        不是确认时刻——用户可能隔很久才点确认，用 at 当锚点会让下一轮把老成交
        重新报成新增（见 app.ack_new_deals）。价格那一段认不出数字时给 None：
        基线里没有数值，累积那边就退化成"不比"，总好过拿猜出来的数当基准。

        跟 set_favorite 同一类：这是用户意图，抓取一个字都不碰它（见 upsert_item
        的列清单）。区别在于确认是**全局**的——一次把所有行的基线一起挪到当前，
        不做逐件确认（见 app.OnAcknowledge）。
        """
        self._begin()
        at = _clean_cached_text(at)
        if at is None:
            self._note("确认时刻不合法，已跳过基线写入")
            return
        rows = []
        for entry in entries or []:
            if not isinstance(entry, (tuple, list)) or len(entry) != 7:
                self._note("确认基线里的条目形状不对，已跳过")
                continue
            cluster_id = _clean_id(entry[0])
            if cluster_id is None:
                self._note("确认基线里的 clusterId 不合法，已跳过")
                continue
            (
                _,
                price_text,
                price_value,
                sold_out,
                lowest_price,
                deals_json,
                deals_updated_at,
            ) = entry
            rows.append(
                (
                    _clean_cached_text(price_text),
                    _clean_amount(price_value),
                    int(bool(sold_out)),
                    _clean_cached_text(lowest_price),
                    _snapshot_json(deals_json),
                    _clean_cached_text(deals_updated_at),
                    at,
                    cluster_id,
                )
            )

        with self.conn:
            for row in rows:
                self.conn.execute(
                    """UPDATE items
                          SET ack_price_text   = ?,
                              ack_price_value  = ?,
                              ack_sold_out     = ?,
                              ack_lowest_price = ?,
                              ack_deals_json   = ?,
                              ack_deals_updated_at = ?,
                              ack_at           = ?
                        WHERE cluster_id = ?""",
                    row,
                )
            # 全局那个时刻跟这组基线同进同出：只写了基线没写时刻（或反过来），
            # 顶部那句「已确认 · 3 小时前」就会指着一次并不完整的确认
            self.conn.execute(_SETTING_SQL, (ACK_AT_SETTING, at))

    def init_ack_if_empty(self, cluster_id, price_text, price_value, sold_out,
                          lowest_price, deals=None, deals_updated_at=None):
        """首次观察到某商品时记一份基线；已经有基线就一个字节都不动。

        跟 acknowledge 的分工有两处：这里只写**还没有基线**的行（WHERE 里判
        ack_at 为空，同一轮里同一商品也只该走一次），而且不碰全局的 last_ack_at
        ——用户什么也没确认过，顶部那句该停在「尚未确认」，不该因为刚加了件商品
        就变成「已确认」。

        deals_updated_at 是这组成交抓回来的时刻，也就是确认基线里那份成交快照
        的锚点（理由同 acknowledge：不能拿写基线的时刻当锚点）。老调用方不给时
        按写基线的时刻算，跟以前一样。

        记下第一次抓到的样子，是为了下一轮有"相对什么"可比（见 app.BuildAckSummary）：
        首次观察本身不算一条变化，这跟 price_change 那边"第一次抓到不报涨跌"
        是同一条规矩。
        """
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            self._note("首次观察的 clusterId 不合法，已跳过基线写入")
            return
        with self.conn:
            self.conn.execute(
                """UPDATE items
                      SET ack_price_text   = ?,
                          ack_price_value  = ?,
                          ack_sold_out     = ?,
                          ack_lowest_price = ?,
                          ack_deals_json   = ?,
                          ack_deals_updated_at = ?,
                          ack_at           = ?
                    WHERE cluster_id = ? AND (ack_at IS NULL OR ack_at = '')""",
                (
                    _clean_cached_text(price_text),
                    _clean_amount(price_value),
                    int(bool(sold_out)),
                    _clean_cached_text(lowest_price),
                    _snapshot_json(deals),
                    _clean_cached_text(deals_updated_at),
                    _now(),
                    cluster_id,
                ),
            )

    def set_favorite(self, cluster_id: str, favorite):
        """手动打 / 取消收藏（items.favorite 存 0/1）。

        这一列不跟着抓取走（见 upsert_item 的列清单），只由用户点星和清单同步
        两处写：它是用户意图，抓回来的东西盖不掉它。

        跟 upsert_item 不同，这里没有"补一条"的语义：记录不存在时这句 UPDATE
        等于没写。缓存以清单为准，清单里有的商品在 sync 之后一定在缓存里，
        点星点的是屏幕上那一行，不存在"查无此人"。
        """
        self._begin()
        cluster_id = _clean_id(cluster_id)
        if cluster_id is None:
            self._note("收藏的 clusterId 不合法，已跳过写入")
            return
        with self.conn:
            self.conn.execute(
                "UPDATE items SET favorite = ? WHERE cluster_id = ?",
                (int(bool(favorite)), cluster_id),
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
            self.conn.execute(_SETTING_SQL, (key, str(value)))


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
    """拆开 (clusterId, 名称[, 收藏]) 并校验 ID；形状不对返回 (None, "", False)。

    收藏可以省略（等同于 False）：老调用方递两元组，以及只关心名字的那些路径。
    """
    if not isinstance(item, (tuple, list)):
        return None, "", False  # 字符串会被逐字拆开，正好挡在门外
    if len(item) == 2:
        cluster_id, name = item
        favorite = False
    elif len(item) == 3:
        cluster_id, name, favorite = item
    else:
        return None, "", False
    return _clean_id(cluster_id), name, bool(favorite)


def _clean_name(name):
    """名字归一：非字符串或空白按"没有"处理（None 才会走 COALESCE 保留旧值）。"""
    if not isinstance(name, str):
        return None
    text = " ".join(name.split())
    return text or None


def _clean_count(value):
    """剩余件数归一：只认正整数，其余按"这次没抓到"处理（存 NULL，保留上次的值）。

    "还剩 0 件"和"接口没报件数"对界面是同一件事（都不显示），归到一起，
    省得 0 被当成一个真件数存进来、下次显示成「 · 0件」。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _clean_amount(value):
    """确认基线里的价格数值（REAL 列）：认得出的数字才写，其余按"没记下"处理。

    跟别处一样不猜：认不出来的就存 NULL，判定那边（见 app.ack_change）据此退化成
    "不比"。bool 是 int 的子类，得单独挡掉，别让 True 变成 1.0。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _snapshot_json(value):
    """确认基线里那组成交：给列表就按标准格式序列化，给文本就照用。

    两条路都是调用方那边就定下来的形状——acknowledge 递的是缓存里 deals_json
    那格原文（原样搬过来，不必解析再拼一遍），init_ack_if_empty 递的是这次抓到的
    成交列表。合在一处，省得每个调用点各判一次。
    """
    if isinstance(value, list):
        return _deals_json(value)
    return _clean_cached_text(value)


def _deals_json(deals):
    """成交列表存成 JSON 文本；None 表示"这次没抓到"（存 NULL，保留上次的）。

    只留认得出的字段：多出来的键存进去也能读回来，但那些字段判「是不是同一条」
    时用不上，白白让缓存变大。空列表照实存成 "[]"——它跟 None 是两回事。
    """
    if deals is None:
        return None
    if not isinstance(deals, list):
        return None
    rows = [
        {"price": str(deal["price"]), "time": str(deal.get("time") or "")}
        for deal in deals
        if isinstance(deal, dict) and deal.get("price")
    ]
    return json.dumps(rows, ensure_ascii=False)


def _dicts(items):
    """从缓存里读回来的东西中挑出成形的 dict（JSON 是外部输入，什么都可能在里面）。"""
    return [item for item in items or [] if isinstance(item, dict)]


def cached_deals_of(record):
    """从一条缓存记录里取 (成交列表, 抓取时刻)；没有基准时是 ([], None)。

    跟 Store.cached_deals() 是同一条路，区别只在这边拿的是已经查出来的记录——
    界面每重画一行都攥着 record 了，不必为同一件事再查一次库（见 app.FillRow）。
    """
    raw = (record or {}).get("deals_json")
    if not isinstance(raw, str) or not raw:
        return [], None
    try:
        deals = json.loads(raw)
    except ValueError:
        return [], None
    if not isinstance(deals, list):
        return [], None
    deals = _dicts(deals)
    if not deals:
        return [], None  # 解析出来是空的（"[]"、[1,2,3] 这类）也算没有基准
    anchor = record.get("deals_updated_at")
    return deals, anchor if isinstance(anchor, str) and anchor else None


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
