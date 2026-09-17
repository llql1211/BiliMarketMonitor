"""store 的回归测试：建表、sync 语义、upsert 保留旧值、排序与设置读写。"""

import sqlite3

import pytest

import store


@pytest.fixture
def db(tmp_path):
    """一个独立的临时缓存库路径。"""
    return str(tmp_path / "cache.db")


# ---------------- 建表与重开 ----------------


def test_init_creates_tables(db):
    """新建 Store 时 items 和 settings 两张表都要建好。"""
    st = store.Store(db)
    names = {
        row[0]
        for row in st.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"items", "settings"} <= names


def test_reopen_same_db_no_error(db):
    """同一个 db 文件再开一个 Store 不报错（CREATE TABLE IF NOT EXISTS）。"""
    store.Store(db)
    st2 = store.Store(db)
    st2.set_setting("k", "v")
    assert st2.get_setting("k") == "v"


# ---------------- sync ----------------


def test_sync_adds_new_items(db):
    """清单里有、缓存里没有的记录被补上，返回 (added, removed)。"""
    st = store.Store(db)
    added, removed = st.sync([("1001", "甲"), ("1002", "乙")])
    assert (added, removed) == (2, 0)
    assert st.get_item("1001")["name"] == "甲"


def test_sync_removes_items_not_in_list(db):
    """缓存里有、清单里没有的记录被删除，removed 计数正确。"""
    st = store.Store(db)
    st.sync([("1001", "甲"), ("1002", "乙"), ("1003", "丙")])
    added, removed = st.sync([("1001", "甲")])
    assert (added, removed) == (0, 2)
    assert st.get_item("1002") is None
    assert st.get_item("1003") is None


def test_sync_does_not_overwrite_existing_name(db):
    """清单里的名字只用来补空，已抓到的真名不被清单旧名覆盖。"""
    st = store.Store(db)
    st.upsert_item("1001", "真名", "img")
    st.sync([("1001", "清单里的旧名")])
    assert st.get_item("1001")["name"] == "真名"
    assert st.get_item("1001")["image_url"] == "img"


def test_sync_fills_empty_name_and_keeps_image(db):
    """补名只写 name 列，已有的 image_url 不受影响。"""
    st = store.Store(db)
    st.sync([("1001", "")])
    st.upsert_item("1001", None, "img")
    st.sync([("1001", "补上的名字")])
    row = st.get_item("1001")
    assert row["name"] == "补上的名字"
    assert row["image_url"] == "img"


def test_sync_empty_list_clears_all(db):
    """sync([]) 以空清单为准，整库清空。"""
    st = store.Store(db)
    st.sync([("1001", "甲"), ("1002", "乙")])
    added, removed = st.sync([])
    assert (added, removed) == (0, 2)
    assert st.get_all() == []


# ---------------- upsert_item ----------------


def test_upsert_item_inserts_new_record(db):
    """新 cluster_id 直接插入。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", "https://img/a.jpg")
    row = st.get_item("1001")
    assert row["name"] == "甲"
    assert row["image_url"] == "https://img/a.jpg"
    assert row["updated_at"]


def test_upsert_item_none_preserves_old_values(db):
    """name/image_url 传 None 时保留旧值（COALESCE 语义）。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", "https://img/a.jpg")
    st.upsert_item("1001", None, None)
    row = st.get_item("1001")
    assert row["name"] == "甲"
    assert row["image_url"] == "https://img/a.jpg"


def test_upsert_item_updates_with_new_values(db):
    """传了新值就更新旧值。"""
    st = store.Store(db)
    st.upsert_item("1001", "旧名", "https://img/old.jpg")
    st.upsert_item("1001", "新名", "https://img/new.jpg")
    row = st.get_item("1001")
    assert row["name"] == "新名"
    assert row["image_url"] == "https://img/new.jpg"


# ---------------- 价格快照 ----------------


def test_upsert_item_saves_price_snapshot(db):
    """抓到价格时，价格几列连同 price_updated_at 一起写进去。"""
    st = store.Store(db)
    st.upsert_item(
        "1001", "甲", None, price_text="¥44", reference_price="¥99", avg_text="¥40"
    )
    row = st.get_item("1001")
    assert row["price_text"] == "¥44"
    assert row["reference_price"] == "¥99"
    assert row["avg_text"] == "¥40"
    assert row["price_updated_at"] == row["updated_at"]


def test_upsert_item_saves_sold_out_flag(db):
    """售罄标记按 0/1 存，取出来是整数。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", None, price_text="¥138", sold_out=True)
    st.upsert_item("1002", "乙", None, price_text="¥44", sold_out=False)
    assert st.get_item("1001")["sold_out"] == 1
    assert st.get_item("1002")["sold_out"] == 0


def test_upsert_without_price_keeps_the_whole_snapshot(db, monkeypatch):
    """没抓到价格时整组原样保留，连 price_updated_at 都不动。"""
    stamps = iter(["2024-01-01T10:00:00", "2024-01-02T10:00:00"])
    monkeypatch.setattr(store, "_now", lambda: next(stamps))
    st = store.Store(db)
    st.upsert_item(
        "1001",
        "甲",
        None,
        price_text="¥44",
        reference_price="¥99",
        avg_text="¥40",
        sold_out=True,
    )

    st.upsert_item("1001", "甲", None)  # 这次没抓到价格

    row = st.get_item("1001")
    assert row["price_text"] == "¥44"
    assert row["reference_price"] == "¥99"
    assert row["avg_text"] == "¥40"
    assert row["sold_out"] == 1
    assert row["price_updated_at"] == "2024-01-01T10:00:00"
    assert row["updated_at"] == "2024-01-02T10:00:00"  # 名称那组照常更新


def test_upsert_with_price_replaces_the_whole_snapshot(db):
    """抓到价格时整组按这次的写：这次没有的字段就存空，不留上次的旧值。

    上次的参考价跟这次的新现价摆在一起，算出来的折扣是错的。
    """
    st = store.Store(db)
    st.upsert_item(
        "1001",
        "甲",
        None,
        price_text="¥44",
        reference_price="¥99",
        avg_text="¥40",
        sold_out=True,
    )

    st.upsert_item("1001", "甲", None, price_text="¥38")

    row = st.get_item("1001")
    assert row["price_text"] == "¥38"
    assert row["reference_price"] is None
    assert row["avg_text"] is None
    assert row["sold_out"] == 0


@pytest.mark.parametrize("blank", ["", "   ", None, 44])
def test_upsert_blank_price_is_treated_as_missing(db, blank):
    """价格是空串或非字符串时按「没抓到」处理，不会冲掉已有快照。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", None, price_text="¥44", sold_out=True)

    st.upsert_item("1001", "甲", None, price_text=blank)

    row = st.get_item("1001")
    assert row["price_text"] == "¥44"
    assert row["sold_out"] == 1


# ---------------- 老缓存库补列 ----------------


def test_old_db_without_price_columns_is_upgraded(db):
    """老库（只有四列）打开时自动补上价格列，且原有数据不受影响。"""
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            """CREATE TABLE items (
                   cluster_id TEXT PRIMARY KEY,
                   name       TEXT,
                   image_url  TEXT,
                   updated_at TEXT
               )"""
        )
        conn.execute("INSERT INTO items VALUES ('1001', '甲', 'img', '2024-01-01T00:00:00')")
    conn.close()

    st = store.Store(db)

    assert st.get_item("1001")["name"] == "甲"  # 老数据还在
    assert st.get_item("1001")["price_text"] is None
    st.upsert_item("1001", "甲", None, price_text="¥44")  # 缺列时写入会整条失败
    assert st.get_item("1001")["price_text"] == "¥44"
    assert st.notes == []  # 补列不该给用户报错


def test_ensure_item_columns_is_idempotent(db):
    """补列跑第二遍不会报错，也不会把列补重。"""
    store.Store(db).close()
    st = store.Store(db)
    st.upsert_item("1001", "甲", None, price_text="¥44")
    columns = [row["name"] for row in st.conn.execute("PRAGMA table_info(items)")]
    assert columns == [name for name, _ in store.ITEM_COLUMNS]


# ---------------- get_item / delete_item ----------------


def test_get_item_missing_returns_none(db):
    """查不存在的 cluster_id 返回 None。"""
    assert store.Store(db).get_item("404") is None


def test_delete_item(db):
    """删除后查不到；删除不存在的记录也不报错。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", None)
    st.delete_item("1001")
    assert st.get_item("1001") is None
    st.delete_item("1001")  # 不抛异常即可


# ---------------- get_all 排序 ----------------


def test_get_all_ordered_by_updated_at_desc(db, monkeypatch):
    """get_all 按 updated_at 倒序（新抓取的在前）。"""
    stamps = iter(["2024-01-01T00:00:01", "2024-01-01T00:00:02", "2024-01-01T00:00:03"])
    monkeypatch.setattr(store, "_now", lambda: next(stamps))
    st = store.Store(db)
    st.upsert_item("1001", "最早", None)
    st.upsert_item("1002", "中间", None)
    st.upsert_item("1003", "最新", None)
    assert [r["cluster_id"] for r in st.get_all()] == ["1003", "1002", "1001"]


# ---------------- settings ----------------


def test_get_setting_default(db):
    """get_setting 找不到时返回传入的默认值，没传则是 None。"""
    st = store.Store(db)
    assert st.get_setting("missing") is None
    assert st.get_setting("missing", "兜底") == "兜底"


def test_set_setting_roundtrip_and_overwrite(db):
    """set 之后能读回，重复 set 覆盖旧值。"""
    st = store.Store(db)
    st.set_setting("theme", "dark")
    assert st.get_setting("theme") == "dark"
    st.set_setting("theme", "light")
    assert st.get_setting("theme") == "light"


def test_settings_keys_independent(db):
    """两个 key 互不干扰。"""
    st = store.Store(db)
    st.set_setting("a", "1")
    st.set_setting("b", "2")
    assert st.get_setting("a") == "1"
    assert st.get_setting("b") == "2"


# ---------------- 持久化 ----------------


def test_data_survives_reopen(db):
    """关掉再打开（同一文件新建 Store）数据还在。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", "img")
    st.set_setting("key", "value")
    st.conn.close()

    st2 = store.Store(db)
    assert st2.get_item("1001")["name"] == "甲"
    assert st2.get_setting("key") == "value"


# ---------------- 空串不能冲掉已有数据 ----------------


def test_upsert_empty_string_preserves_old_values(db):
    """空串要按「没抓到」处理：SQLite 里空串不是 NULL，COALESCE 挡不住它。"""
    st = store.Store(db)
    st.upsert_item("1001", "真名", "https://img/a.jpg")

    st.upsert_item("1001", "", "")

    row = st.get_item("1001")
    assert row["name"] == "真名"
    assert row["image_url"] == "https://img/a.jpg"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_upsert_blank_values_keep_old(db, blank):
    """空白字符串和 None 一样，都保留旧值。"""
    st = store.Store(db)
    st.upsert_item("1001", "真名", "https://img/a.jpg")
    st.upsert_item("1001", blank, blank)
    row = st.get_item("1001")
    assert row["name"] == "真名"
    assert row["image_url"] == "https://img/a.jpg"


@pytest.mark.parametrize("image_url", [123, {"a": 1}, ["x"], b"bytes"])
def test_upsert_non_string_image_is_dropped(db, image_url):
    """image_url 不是字符串时按「没有」处理：界面会拿它去下载图片，不能是数字。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", image_url)
    assert st.get_item("1001")["image_url"] is None


# ---------------- 入参自带检测 ----------------


@pytest.mark.parametrize("cluster_id", [None, "", "abc", True, "1001;"])
def test_upsert_illegal_id_is_skipped_with_a_note(db, cluster_id):
    """clusterId 非法时不写库，只记一条 notes（notes 属于「本次操作」，要紧接着读）。"""
    st = store.Store(db)
    st.upsert_item(cluster_id, "甲", None)

    assert len(st.notes) == 1 and "clusterId" in st.notes[0]
    assert st.get_all() == []


def test_get_and_delete_illegal_id_are_noops(db):
    """查/删用的 clusterId 非法时当没查到处理，不抛异常。"""
    st = store.Store(db)
    st.upsert_item("1001", "甲", None)
    assert st.get_item(None) is None
    assert st.get_item("abc") is None
    st.delete_item(None)
    st.delete_item("abc")
    assert st.get_item("1001") is not None


@pytest.mark.parametrize(
    "item",
    ["1001", ("1001",), ("1001", "甲", "多余"), ("abc", "甲"), (None, "甲"), None],
)
def test_sync_skips_malformed_items(db, item):
    """条目形状不对时跳过并记 notes，同批里其他条目照常同步。"""
    st = store.Store(db)
    added, removed = st.sync([item, ("1002", "乙")])

    assert (added, removed) == (1, 0)
    assert len(st.notes) == 1 and "跳过" in st.notes[0]  # 紧接着读，别被下次操作清掉
    assert [r["cluster_id"] for r in st.get_all()] == ["1002"]


def test_sync_none_means_empty_list(db):
    """sync(None) 与 sync([]) 同义：以空清单为准，整库清空。"""
    st = store.Store(db)
    st.sync([("1001", "甲")])
    assert st.sync(None) == (0, 1)
    assert st.get_all() == []


def test_notes_are_reset_per_operation(db):
    """notes 是「本次操作」的提示，不该一直累积。"""
    st = store.Store(db)
    st.upsert_item("abc", "甲", None)
    assert st.notes
    st.get_all()
    assert st.notes == []


def test_set_setting_rejects_bad_input(db):
    """键值类型不对时跳过写入并记 notes，不抛异常。"""
    st = store.Store(db)
    st.set_setting("theme", None)
    assert st.notes
    st.set_setting(123, "dark")
    assert st.notes

    assert st.get_setting("theme") is None
    assert st.get_setting(123, "兜底") == "兜底"  # 非字符串 key 当查不到


# ---------------- 缓存库坏了也要能起来 ----------------


def test_corrupt_db_is_backed_up_and_rebuilt(db, tmp_path):
    """cache.db 是垃圾字节时：改名备份 + 重建空库，程序照常起来。"""
    with open(db, "w", encoding="utf-8") as f:
        f.write("这不是一个 SQLite 文件" * 10)

    st = store.Store(db)

    backups = list(tmp_path.glob("cache.db.bad-*"))
    assert len(backups) == 1
    assert "SQLite" in backups[0].read_text(encoding="utf-8")  # 坏文件留着排查
    assert any("损坏" in note for note in st.notes)
    # 新库可用
    st.upsert_item("1001", "甲", None)
    assert st.get_item("1001")["name"] == "甲"


def test_corrupt_db_keeps_working_after_restart(db, tmp_path):
    """重建过一次之后，再打开不会再产生第二份备份。"""
    with open(db, "w", encoding="utf-8") as f:
        f.write("garbage")
    st = store.Store(db)
    st.close()

    st2 = store.Store(db)
    st2.set_setting("k", "v")
    assert st2.get_setting("k") == "v"
    assert len(list(tmp_path.glob("cache.db.bad-*"))) == 1
    assert st2.notes == []


def test_store_creates_missing_parent_directory(tmp_path):
    """data/ 被删掉（或首次运行）时自己建目录，不让程序起不来。"""
    path = str(tmp_path / "gone" / "deeper" / "cache.db")
    st = store.Store(path)
    st.upsert_item("1001", "甲", None)
    assert st.get_item("1001")["name"] == "甲"


def test_store_survives_path_occupied_by_a_directory(tmp_path):
    """路径被别的目录占住时也要能起来：能改名就重建，不行就退化成内存缓存。

    这里不限定走哪条降级路径，只要求「有提示 + 功能可用」。
    """
    path = str(tmp_path / "cache.db")
    (tmp_path / "cache.db").mkdir()

    st = store.Store(path)
    assert st.notes  # 打开时就有降级提示
    st.upsert_item("1001", "甲", None)
    assert st.get_item("1001")["name"] == "甲"


def test_close_is_idempotent_and_blocks_further_use(db):
    """close() 可以重复调用；关掉之后再操作要给一句清楚的报错，而不是 sqlite3 的怪话。"""
    st = store.Store(db)
    st.close()
    st.close()

    with pytest.raises(RuntimeError):
        st.get_all()


def test_context_manager_closes(db):
    """支持 with：出了作用域连接就关掉。"""
    with store.Store(db) as st:
        st.upsert_item("1001", "甲", None)
        assert st.get_item("1001")["name"] == "甲"
    assert st.conn is None
