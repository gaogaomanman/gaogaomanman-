"""抽采样进度统计 · 存储层（store）单元测试。

覆盖：建表幂等、区域/产品类型/合同 CRUD、唯一约束、删除保护（被引用不可删）、
任务量 upsert 与整批回滚、完成量快照整年替换与失败不破坏旧数据、同步日志读写。

所有用例都落到 `tmp_path`（不污染 `backend/data/progress`）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import settings
from app.modules.progress import store


@pytest.fixture()
def st(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "progress_dir", str(tmp_path), raising=False)
    return store


# ==================== 建表 ====================


def test_schema_created_and_idempotent(st) -> None:
    conn = st._connect()
    conn.close()
    conn = st._connect()  # 第二次连接不应报错（幂等）
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"contract", "region", "product_type", "task_quota", "done_snapshot", "sync_log"} <= names
    assert set(st.stats()) >= {"region", "contract", "product_type", "task_quota", "done_snapshot", "sync_log"}


# ==================== 产品类型 ====================


def test_product_type_crud_and_unique(st) -> None:
    a = st.create_product_type("豇豆", members="长豇豆|豆角", category="蔬菜")
    assert a["sort_no"] == 10
    b = st.create_product_type("辣椒", category="蔬菜")
    assert b["sort_no"] == 20

    with pytest.raises(ValueError):
        st.create_product_type("豇豆")

    st.update_product_type(a["id"], category="叶菜", enabled=False)
    row = st.get_product_type(a["id"])
    assert row["category"] == "叶菜" and row["enabled"] == 0
    # 改名不影响历史数据（靠 id 关联）
    st.update_product_type(a["id"], name="豇豆（新）")
    assert st.get_product_type(a["id"])["name"] == "豇豆（新）"

    # 上移/下移按 (sort_no, id) 交换
    items = st.move_product_type(b["id"], "up")
    assert [i["id"] for i in items] == [b["id"], a["id"]]

    st.delete_product_type(a["id"])
    assert st.get_product_type(a["id"]) is None


def test_product_type_members_roundtrip(st) -> None:
    """成员品种（组合口径）的读写：不传就不动、传了就覆盖。"""
    row = st.create_product_type("七条鱼", members="鲫鱼|鳊鱼|鲈鱼", category="水产品")
    assert row["members"] == "鲫鱼|鳊鱼|鲈鱼"
    st.update_product_type(row["id"], members="鲫鱼|鳊鱼")
    assert st.get_product_type(row["id"])["members"] == "鲫鱼|鳊鱼"
    st.update_product_type(row["id"], category="水产品-专项")  # 不动 members
    assert st.get_product_type(row["id"])["members"] == "鲫鱼|鳊鱼"
    assert st.find_product_type_by_name("七条鱼")["id"] == row["id"]
    assert st.find_product_type_by_name("不存在") is None


def test_migration_moves_old_alias_to_members(st) -> None:
    """老库只有 alias 列：首次连接自动补 members 并把 alias 值搬过来，且**只搬一次**。"""
    import sqlite3

    path = st.db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE product_type (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "alias TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', "
        "sort_no INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, "
        "updated_at TEXT NOT NULL DEFAULT '');"
        "INSERT INTO product_type (name, alias, category) VALUES ('其他蔬菜', '蔬菜|蔬果', '农产品');"
    )
    conn.commit()
    conn.close()

    rows = st.list_product_types()
    assert rows[0]["members"] == "蔬菜|蔬果"

    st.update_product_type(int(rows[0]["id"]), members="")
    assert st.get_product_type(int(rows[0]["id"]))["members"] == ""


def test_product_type_delete_blocked_when_referenced(st) -> None:
    pt = st.create_product_type("豇豆")
    st.save_quotas(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": pt["id"], "quota": 5}])
    with pytest.raises(ValueError, match="任务量"):
        st.delete_product_type(pt["id"])

    # 快照引用同样保护
    pt2 = st.create_product_type("辣椒")
    st.replace_done_snapshot(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": pt2["id"], "done": 3}], "t")
    with pytest.raises(ValueError, match="快照"):
        st.delete_product_type(pt2["id"])


# ==================== 区域 ====================


def test_region_ensure_enable_and_delete_protection(st) -> None:
    assert st.ensure_regions(["常熟市", "昆山市"], source="base") == 2
    assert st.ensure_regions(["常熟市", "六合区"]) == 1  # 已存在不重复登记
    st.ensure_regions(["六合区"])  # 幂等

    st.set_region_enabled("六合区", False)
    names = {r["name"]: r for r in st.list_regions(include_disabled=True)}
    assert names["六合区"]["enabled"] == 0
    assert all(r["name"] != "六合区" for r in st.list_regions(include_disabled=False))

    st.save_quotas(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": 1}])
    with pytest.raises(ValueError, match="任务量"):
        st.delete_region("常熟市")
    st.delete_region("六合区")
    assert "六合区" not in {r["name"] for r in st.list_regions(include_disabled=True)}


# ==================== 合同 ====================


def test_contract_manual_and_enable(st) -> None:
    row = st.upsert_contract("C1", name="市例行", note="备注")
    assert row["source"] == "manual" and row["name_locked"] == 1
    st.set_contract_enabled("C1", False)
    assert st.get_contract("C1")["enabled"] == 0
    st.upsert_contract("C1", name="市例行", enabled=True)
    assert st.get_contract("C1")["enabled"] == 1

    with pytest.raises(ValueError, match="不存在"):
        st.set_contract_enabled("NOPE", True)
    with pytest.raises(ValueError):
        st.upsert_contract("   ")


def test_contract_auto_upsert_never_overwrites_manual_name(st) -> None:
    st.upsert_auto_contracts([("C1", "任务A"), ("C2", "")])
    assert st.get_contract("C1")["name"] == "任务A"
    assert st.get_contract("C2")["source"] == "auto"

    # 人工改名 → 加锁
    st.upsert_contract("C1", name="市例行合同", enabled=True)
    st.upsert_auto_contracts([("C1", "任务A-改名版")])
    assert st.get_contract("C1")["name"] == "市例行合同"
    assert st.get_contract("C1")["source"] == "manual"

    # 未加锁且为空的，自动发现可以补名称
    st.upsert_auto_contracts([("C2", "任务B")])
    assert st.get_contract("C2")["name"] == "任务B"

    # 新增计数只算真正插入的
    assert st.upsert_auto_contracts([("C1", "x"), ("C3", "任务C")]) == 1


def test_contract_update_without_name_keeps_existing(st) -> None:
    """编辑合同时不提交 name（页面已不展示名称）→ 不得把已有值清空。"""
    st.upsert_contract("C1", name="人工起的名字", note="旧备注")
    st.upsert_contract("C1", note="新备注")
    row = st.get_contract("C1")
    assert row["name"] == "人工起的名字" and row["note"] == "新备注"

    # 显式传空串才算清空（保留该能力，供以后需要时使用）
    st.upsert_contract("C1", name="")
    assert st.get_contract("C1")["name"] == ""


def test_contract_delete_rules(st) -> None:
    st.upsert_auto_contracts([("AUTO1", "任务")])
    with pytest.raises(ValueError, match="自动发现"):
        st.delete_contract("AUTO1")

    st.upsert_contract("M1", name="手工合同")
    st.delete_contract("M1")
    assert st.get_contract("M1") is None

    st.upsert_contract("M2", name="手工合同2")
    st.save_quotas(2026, [{"contract_no": "M2", "region": "常熟市", "product_type_id": 1, "quota": 2}])
    with pytest.raises(ValueError, match="任务量"):
        st.delete_contract("M2")


# ==================== 任务量 ====================


def test_save_quotas_upsert_and_clear(st) -> None:
    st.save_quotas(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": 10}])
    st.save_quotas(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": 12}])
    rows = st.read_quotas(2026, "C1")
    assert len(rows) == 1 and rows[0]["quota"] == 12  # upsert 不产生重复行
    assert st.read_quotas(2026, "C2") == []
    assert st.clear_quotas(2026, "C1") == 1
    assert st.read_quotas(2026) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": -1},
        {"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": "abc"},
        {"contract_no": "C1", "region": "", "product_type_id": 1, "quota": 1},
        {"contract_no": "C1", "region": "常熟市", "product_type_id": None, "quota": 1},
    ],
)
def test_save_quotas_rejects_bad_rows(st, bad) -> None:
    with pytest.raises(ValueError):
        st.save_quotas(2026, [bad])
    assert st.read_quotas(2026) == []


def test_save_quotas_is_all_or_nothing(st) -> None:
    """整批一次事务：一条非法 → 之前合法的也不落库（避免"改了一半"）。"""
    st.save_quotas(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": 3}])
    with pytest.raises(ValueError):
        st.save_quotas(
            2027,
            [
                {"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "quota": 9},
                {"contract_no": "C1", "region": "昆山市", "product_type_id": 1, "quota": -9},
            ],
        )
    assert st.read_quotas(2027) == []


# ==================== 完成量快照 ====================


def test_replace_done_snapshot_and_read(st) -> None:
    st.replace_done_snapshot(
        2026,
        [
            {"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "done": 4},
            {"contract_no": "", "region": "常熟市", "product_type_id": 1, "done": 2},
        ],
        "2026-09-21 10:00:00",
    )
    rows = st.read_done_snapshot(2026)
    assert len(rows) == 2 and all(r["synced_at"] == "2026-09-21 10:00:00" for r in rows)
    assert st.done_snapshot_totals(2026) == {"C1": 4, "": 2}

    # 整年替换：旧数据被清掉，不会累积
    st.replace_done_snapshot(2026, [{"contract_no": "C1", "region": "昆山市", "product_type_id": 1, "done": 1}], "t2")
    rows = st.read_done_snapshot(2026)
    assert len(rows) == 1 and rows[0]["region"] == "昆山市"
    # 其他年份不受影响
    assert st.read_done_snapshot(2025) == []


def test_replace_done_snapshot_failure_keeps_old_data(st) -> None:
    st.replace_done_snapshot(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": 1, "done": 7}], "t1")
    with pytest.raises((ValueError, TypeError)):
        st.replace_done_snapshot(2026, [{"contract_no": "C1", "region": "常熟市", "product_type_id": "abc", "done": 1}], "t2")
    rows = st.read_done_snapshot(2026)
    assert len(rows) == 1 and rows[0]["done"] == 7  # 旧快照完好


def test_year_out_of_range(st) -> None:
    with pytest.raises(ValueError):
        st.read_quotas(1900)


# ==================== 同步日志 ====================


def test_sync_log_write_and_list(st) -> None:
    log_id = st.write_sync_log(
        {
            "year": 2026, "started_at": "2026-09-21 10:00:00", "finished_at": "2026-09-21 10:00:01",
            "source": "mirror", "data_deadline": "2026-09-20T19:01:16", "rows": 100, "done_total": 90,
            "contract_count": 3, "dup_across_contracts": 0, "unmatched": 5, "unclassified": 2,
            "no_contract": 1, "excluded": 2, "elapsed_ms": 120, "status": "ok", "error": "",
        }
    )
    assert log_id > 0
    st.write_sync_log({"year": 2026, "status": "failed", "error": "boom"})
    logs = st.list_sync_logs(2026, 10)
    assert len(logs) == 2 and logs[0]["status"] == "failed"
    assert st.last_sync(2026)["status"] == "failed"
    assert st.last_sync(2026, only_ok=True)["status"] == "ok"
    assert st.last_sync(2025) is None


# ==================== 元信息 ====================


def test_meta_roundtrip_keeps_long_json(st) -> None:
    import json

    payload = json.dumps([[f"样品名{i}", i] for i in range(200)], ensure_ascii=False)
    st.set_meta("unmatched_names_2026", payload)
    assert json.loads(st.get_meta("unmatched_names_2026"))[199][0] == "样品名199"


def test_db_file_under_configured_dir(st, tmp_path) -> None:
    assert st.db_file() == tmp_path / "progress.sqlite"
    assert isinstance(st.stats(), dict)


def test_connect_survives_existing_file(st) -> None:
    """文件已存在时重复初始化不应破坏数据（页面反复刷新/多进程场景）。"""
    st.create_product_type("豇豆")
    for _ in range(3):
        st._connect().close()
    assert len(st.list_product_types()) == 1
    with sqlite3.connect(str(st.db_file())) as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_type").fetchone()[0] == 1
