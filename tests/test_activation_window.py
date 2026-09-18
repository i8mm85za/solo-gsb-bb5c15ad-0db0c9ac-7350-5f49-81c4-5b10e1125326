"""版本化生效窗口（activation_window）的接口与判定行为测试。"""

import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import SessionLocal
from app.models import FlagConfig, FlagConfigVersion

EVAL = "/api/v1/flags/btn_color/evaluate"


def _flag(
    *,
    activation_window=None,
    rules=None,
    variants=None,
    flag_key="btn_color",
    description="按钮颜色实验",
):
    if variants is None:
        variants = [
            {"key": "red", "weight": 50},
            {"key": "blue", "weight": 50},
        ]
    if rules is None:
        rules = [
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": ["cn"],
                "variant_key": "red",
            }
        ]
    payload = {
        "flag_key": flag_key,
        "description": description,
        "variants": variants,
        "rules": rules,
    }
    if activation_window is not None:
        payload["activation_window"] = activation_window
    return payload


def _future_window(**overrides):
    now = datetime.now(timezone.utc)
    win = {
        "active_from": (now + timedelta(days=1)).isoformat(),
        "active_until": (now + timedelta(days=2)).isoformat(),
        "inactive_variant_key": "blue",
    }
    win.update(overrides)
    return win


def _past_window(**overrides):
    now = datetime.now(timezone.utc)
    win = {
        "active_from": (now - timedelta(days=2)).isoformat(),
        "active_until": (now - timedelta(days=1)).isoformat(),
        "inactive_variant_key": "blue",
    }
    win.update(overrides)
    return win


def _active_window(**overrides):
    now = datetime.now(timezone.utc)
    win = {
        "active_from": (now - timedelta(hours=1)).isoformat(),
        "active_until": (now + timedelta(hours=1)).isoformat(),
        "inactive_variant_key": "blue",
    }
    win.update(overrides)
    return win


# ---------------------------------------------------------------------------
# 创建 / 更新校验与持久化
# ---------------------------------------------------------------------------


def test_create_without_window_defaults_to_null(client, simple_flag):
    body = client.get("/api/v1/flags/btn_color").json()
    assert body["activation_window"] is None
    ver = client.get("/api/v1/flags/btn_color/versions/1").json()
    assert ver["activation_window"] is None


def test_create_with_window_normalizes_offset_bounds_to_utc(client):
    payload = _flag(
        activation_window={
            "active_from": "2026-09-20T16:00:00+08:00",
            "active_until": "2026-09-22T16:00:00+08:00",
            "inactive_variant_key": "blue",
        }
    )
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201, resp.text
    win = resp.json()["activation_window"]
    assert win["inactive_variant_key"] == "blue"
    frm = datetime.fromisoformat(win["active_from"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(win["active_until"].replace("Z", "+00:00"))
    assert frm.utcoffset() == timedelta(0)
    assert until.utcoffset() == timedelta(0)
    assert frm.isoformat() == "2026-09-20T08:00:00+00:00"
    assert until.isoformat() == "2026-09-22T08:00:00+00:00"


def test_window_requires_at_least_one_bound(client):
    resp = client.post(
        "/api/v1/flags", json=_flag(activation_window={"inactive_variant_key": "blue"})
    )
    assert resp.status_code == 422
    assert "至少" in resp.text


def test_window_rejects_naive_bound(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": "2026-09-20T08:00:00",
                "inactive_variant_key": "blue",
            }
        ),
    )
    assert resp.status_code == 422
    assert "时区" in resp.text


def test_window_rejects_from_equal_to_until(client):
    ts = "2026-09-20T08:00:00Z"
    resp = client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": ts,
                "active_until": ts,
                "inactive_variant_key": "blue",
            }
        ),
    )
    assert resp.status_code == 422
    assert "active_from" in resp.text


def test_window_rejects_from_later_than_until(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag(activation_window=_future_window(active_from="2026-09-30T00:00:00Z")),
    )
    assert resp.status_code == 422


def test_window_rejects_unknown_inactive_variant(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag(activation_window=_future_window(inactive_variant_key="ghost")),
    )
    assert resp.status_code == 422
    assert "ghost" in resp.text


def test_only_active_from_is_allowed(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": "2026-09-20T08:00:00Z",
                "inactive_variant_key": "blue",
            }
        ),
    )
    assert resp.status_code == 201, resp.text
    win = resp.json()["activation_window"]
    assert win["active_until"] is None
    assert win["active_from"].endswith("Z")


def test_only_active_until_is_allowed(client):
    resp = client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_until": "2026-09-30T08:00:00Z",
                "inactive_variant_key": "red",
            }
        ),
    )
    assert resp.status_code == 201, resp.text
    win = resp.json()["activation_window"]
    assert win["active_from"] is None
    assert win["inactive_variant_key"] == "red"


def test_window_persisted_with_each_immutable_version(client):
    # v1 无窗口
    client.post("/api/v1/flags", json=_flag())
    # v2 带未来窗口
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    versions = client.get("/api/v1/flags/btn_color/versions").json()
    assert versions[0]["activation_window"] is None
    assert versions[1]["activation_window"]["inactive_variant_key"] == "blue"
    # v1 行不随后续更新改变
    assert versions[0]["version"] == 1 and versions[1]["version"] == 2


def test_update_without_window_removes_window(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    assert client.get("/api/v1/flags/btn_color").json()["activation_window"] is not None
    # 整体覆盖：省略 activation_window 即移除窗口
    resp = client.post("/api/v1/flags", json=_flag())
    assert resp.status_code == 201
    assert resp.json()["version"] == 2
    assert resp.json()["activation_window"] is None
    # 当前配置与新版本行均无窗口，v1 历史仍保留窗口
    versions = client.get("/api/v1/flags/btn_color/versions").json()
    assert versions[0]["activation_window"] is not None
    assert versions[1]["activation_window"] is None


# ---------------------------------------------------------------------------
# 窗口外判定
# ---------------------------------------------------------------------------


def test_before_window_returns_inactive_variant(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    body = client.post(EVAL, json={"user_id": "u1", "context": {"region": "cn"}}).json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "blue"
    assert body["bucket_value"] is None
    assert body["matched_rule"] is None
    assert body["config_version"] == 1
    steps = [e["step"] for e in body["explanation"]]
    assert steps == ["activation_window_loaded", "inactive_selected"]
    loaded = body["explanation"][0]["detail"]
    assert loaded["inactive_variant_key"] == "blue"
    assert loaded["active_from"] is not None
    assert "尚未生效" in body["explanation"][1]["detail"]["reason"]


def test_after_window_returns_inactive_variant(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_past_window()))
    body = client.post(EVAL, json={"user_id": "u1", "context": {}}).json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "blue"
    assert "已失效" in body["explanation"][1]["detail"]["reason"]


def test_only_until_in_past_is_inactive(client):
    now = datetime.now(timezone.utc)
    client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_until": (now - timedelta(minutes=1)).isoformat(),
                "inactive_variant_key": "red",
            }
        ),
    )
    body = client.post(EVAL, json={"user_id": "u1", "context": {}}).json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "red"


def test_inactive_skips_override_too(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    # 即便存在命中规则的 context 与未过期 override，窗口外一律兜底
    put = client.put(
        "/api/v1/flags/btn_color/overrides/u1",
        json={
            "variant_key": "red",
            "reason": "客服补偿",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        },
    )
    assert put.status_code == 200
    body = client.post(EVAL, json={"user_id": "u1", "context": {"region": "cn"}}).json()
    assert body["decision_path"] == "inactive"
    assert body["variant_key"] == "blue"


# ---------------------------------------------------------------------------
# 窗口内判定：保持原语义
# ---------------------------------------------------------------------------


def test_within_window_rule_still_hits(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_active_window()))
    body = client.post(EVAL, json={"user_id": "u1", "context": {"region": "cn"}}).json()
    assert body["decision_path"] == "rule"
    assert body["variant_key"] == "red"


def test_within_window_override_still_applies(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_active_window()))
    client.put(
        "/api/v1/flags/btn_color/overrides/u1",
        json={"variant_key": "blue", "reason": "x"},
    )
    body = client.post(EVAL, json={"user_id": "u1", "context": {}}).json()
    assert body["decision_path"] == "override"
    assert body["variant_key"] == "blue"


def test_only_from_in_past_is_active(client):
    now = datetime.now(timezone.utc)
    client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": (now - timedelta(days=1)).isoformat(),
                "inactive_variant_key": "blue",
            }
        ),
    )
    body = client.post(EVAL, json={"user_id": "u1", "context": {"region": "cn"}}).json()
    assert body["decision_path"] == "rule"


# ---------------------------------------------------------------------------
# 版本钉住：使用所选版本各自的窗口
# ---------------------------------------------------------------------------


def test_pinned_old_version_without_window_ignores_current_window(client):
    # v1 无窗口；v2 配置未来窗口（当前判定应兜底）
    client.post("/api/v1/flags", json=_flag())
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))

    # 默认（最新 v2）：窗口外 -> inactive
    latest = client.post(EVAL, json={"user_id": "u1", "context": {"region": "cn"}}).json()
    assert latest["config_version"] == 2
    assert latest["decision_path"] == "inactive"

    # 钉住 v1：v1 无窗口，老数据视为无窗口，保持原语义
    pinned = client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}, "config_version": 1}
    ).json()
    assert pinned["config_version"] == 1
    assert pinned["decision_path"] == "rule"
    assert pinned["variant_key"] == "red"


def test_pinned_version_uses_its_own_window(client):
    # v1 窗口在未来（窗口外）；v2 去掉窗口
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    client.post("/api/v1/flags", json=_flag())
    # 默认 v2 无窗口 -> 规则命中
    assert client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}}
    ).json()["decision_path"] == "rule"
    # 钉住 v1 -> 仍按 v1 的窗口判定为 inactive
    pinned = client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}, "config_version": 1}
    ).json()
    assert pinned["config_version"] == 1
    assert pinned["decision_path"] == "inactive"
    assert pinned["variant_key"] == "blue"


# ---------------------------------------------------------------------------
# 快照与幂等重放：相同 request_id 不按当前时间重算
# ---------------------------------------------------------------------------


def _set_window_bounds(active_from, active_until):
    """直接改库（绕过接口），模拟时间流逝导致窗口状态翻转。"""
    db = SessionLocal()
    try:
        for model in (FlagConfig, FlagConfigVersion):
            for row in db.query(model).filter_by(flag_key="btn_color").all():
                row.active_from = active_from
                row.active_until = active_until
        db.commit()
    finally:
        db.close()


def test_replay_when_window_flips_active_to_inactive(client):
    now = datetime.now(timezone.utc)
    client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": (now - timedelta(hours=1)).isoformat(),
                "active_until": (now + timedelta(hours=1)).isoformat(),
                "inactive_variant_key": "blue",
            }
        ),
    )
    payload = {"user_id": "u1", "context": {"region": "cn"}, "request_id": "win-rid-1"}
    first = client.post(EVAL, json=payload).json()
    assert first["decision_path"] == "rule"
    assert first["variant_key"] == "red"

    # 时间流逝：窗口整体移到过去 -> 新判定应为 inactive
    _set_window_bounds(now - timedelta(days=2), now - timedelta(days=1))
    normal = client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}}
    ).json()
    assert normal["decision_path"] == "inactive"

    # 同 request_id 直接重放首次快照，不按当前时间重算
    replay = client.post(EVAL, json=payload).json()
    assert replay["request_id"] == "win-rid-1"
    assert replay["decision_path"] == "rule"
    assert replay["variant_key"] == "red"
    assert replay["explanation"] == first["explanation"]


def test_replay_when_window_flips_inactive_to_active(client):
    now = datetime.now(timezone.utc)
    client.post(
        "/api/v1/flags",
        json=_flag(
            activation_window={
                "active_from": (now + timedelta(days=1)).isoformat(),
                "active_until": (now + timedelta(days=2)).isoformat(),
                "inactive_variant_key": "blue",
            }
        ),
    )
    payload = {"user_id": "u1", "context": {"region": "cn"}, "request_id": "win-rid-2"}
    first = client.post(EVAL, json=payload).json()
    assert first["decision_path"] == "inactive"
    assert first["variant_key"] == "blue"

    # 窗口改为覆盖当前 -> 新判定走规则
    _set_window_bounds(now - timedelta(hours=1), now + timedelta(hours=1))
    normal = client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}}
    ).json()
    assert normal["decision_path"] == "rule"

    # 重放仍为首次的 inactive 快照
    replay = client.post(EVAL, json=payload).json()
    assert replay["request_id"] == "win-rid-2"
    assert replay["decision_path"] == "inactive"
    assert replay["variant_key"] == "blue"
    assert replay["explanation"] == first["explanation"]


def test_inactive_snapshot_queryable_and_listed(client):
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    decision = client.post(
        EVAL, json={"user_id": "u1", "context": {}}
    ).json()
    rid = decision["request_id"]

    snap = client.get(f"/api/v1/decisions/{rid}").json()
    assert snap["decision_path"] == "inactive"
    assert snap["variant_key"] == "blue"
    assert snap["bucket_value"] is None
    assert snap["matched_rule_index"] is None
    steps = [e["step"] for e in snap["explanation"]]
    assert steps == ["activation_window_loaded", "inactive_selected"]

    items = client.get(
        "/api/v1/decisions", params={"flag_key": "btn_color", "variant_key": "blue"}
    ).json()["items"]
    assert items[0]["decision_path"] == "inactive"


# ---------------------------------------------------------------------------
# 回滚：窗口随目标历史版本一起复制
# ---------------------------------------------------------------------------


def test_rollback_copies_window_from_target_version(client):
    # v1 带未来窗口；v2 去掉窗口
    client.post("/api/v1/flags", json=_flag(activation_window=_future_window()))
    client.post("/api/v1/flags", json=_flag())
    assert client.get("/api/v1/flags/btn_color").json()["activation_window"] is None

    resp = client.post(
        "/api/v1/flags/btn_color/rollback",
        json={"target_version": 1, "expected_current_version": 2},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 3
    assert body["activation_window"] is not None
    assert body["activation_window"]["inactive_variant_key"] == "blue"

    # 新版本立即按复制来的窗口判定 -> inactive
    decision = client.post(
        EVAL, json={"user_id": "u1", "context": {"region": "cn"}}
    ).json()
    assert decision["config_version"] == 3
    assert decision["decision_path"] == "inactive"


# ---------------------------------------------------------------------------
# 老 SQLite 幂等迁移
# ---------------------------------------------------------------------------


def test_ensure_activation_window_columns_migrates_legacy_sqlite(client):
    path = tempfile.mktemp(prefix="legacy-flags-", suffix=".db")
    legacy_engine = create_engine(f"sqlite:///{path}", future=True)
    with legacy_engine.begin() as conn:
        # 升级前的老表结构：没有任何窗口列
        conn.execute(
            text(
                "CREATE TABLE flag_configs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "flag_key VARCHAR(128) UNIQUE, description VARCHAR(512), "
                "rules_json TEXT, variants_json TEXT, version INTEGER, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE flag_config_versions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "flag_key VARCHAR(128), version INTEGER, description VARCHAR(512), "
                "rules_json TEXT, variants_json TEXT, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO flag_configs (flag_key, description, rules_json, "
                "variants_json, version, created_at, updated_at) "
                "VALUES ('legacy', '', '[]', "
                "'[{\"key\": \"a\", \"weight\": 50}, {\"key\": \"b\", \"weight\": 50}]', "
                "1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
            )
        )

    LegacySession = sessionmaker(bind=legacy_engine, future=True)
    db = LegacySession()
    try:
        # 连续执行两次必须幂等
        crud.ensure_activation_window_columns(db)
        crud.ensure_activation_window_columns(db)

        cols = {c["name"] for c in inspect(db.bind).get_columns("flag_configs")}
        assert {"active_from", "active_until", "inactive_variant_key"} <= cols

        flag = crud.get_flag_or_none(db, "legacy")
        assert flag is not None
        # 老数据视为无窗口
        assert crud.get_window(
            flag.active_from, flag.active_until, flag.inactive_variant_key
        ) is None

        # 补齐缺失版本记录时同样不带窗口
        assert crud.backfill_missing_versions(db) == 1
        ver = crud.get_flag_version(db, "legacy", 1)
        assert ver is not None
        assert ver.inactive_variant_key is None

        # 迁移后可正常写入带窗口的新版本
        from app.schemas import FlagUpsertRequest

        now = datetime.now(timezone.utc)
        payload = FlagUpsertRequest(
            flag_key="legacy",
            variants=[{"key": "a", "weight": 50}, {"key": "b", "weight": 50}],
            rules=[],
            activation_window={
                "active_from": (now + timedelta(days=1)).isoformat(),
                "inactive_variant_key": "b",
            },
        )
        crud.upsert_flag(db, payload)
        flag = crud.get_flag_or_none(db, "legacy")
        assert flag.version == 2
        assert flag.inactive_variant_key == "b"
        assert flag.active_from is not None
    finally:
        db.close()
        legacy_engine.dispose()
