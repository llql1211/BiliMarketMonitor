"""store 的回归测试：建表、sync 语义、upsert 保留旧值、排序与设置读写。"""

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
