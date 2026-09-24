from __future__ import annotations

from app.seismic.service import SeismicService


def event_payload(external_id: str = "EQ-VER-001", **overrides):
    payload = {
        "external_id": external_id,
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": 5.8,
        "magnitude_type": "ML",
        "source": "test",
    }
    payload.update(overrides)
    return payload


def _create_event(client, external_id="EQ-VER-001", **overrides):
    response = client.post("/api/seismic/events", json=event_payload(external_id, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _publish(client, version_id, operator="duty-officer", reason="正式发布", if_match=None, expected=200):
    headers = {"If-Match": f'"{if_match}"'} if if_match else None
    response = client.post(
        f"/api/seismic/parameter-versions/{version_id}/publish",
        json={"reason": reason, "operator": operator},
        headers=headers,
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_create_event_carries_initial_draft_version(client):
    event = _create_event(client)
    assert event["latest_parameter_version"] is not None
    assert event["latest_parameter_version"]["version"] == 1
    assert event["latest_parameter_version"]["state"] == "draft"
    assert event["current_parameter_version"] is None
    assert event["parameter_versions_count"] == 1

    listing = client.get(f"/api/seismic/events/{event['id']}/parameter-versions").json()
    assert listing["current_version_id"] is None
    assert len(listing["items"]) == 1
    assert listing["items"][0]["state"] == "draft"
    assert listing["items"][0]["is_current"] is False
    assert listing["items"][0]["created_by"] == "test"
    assert listing["items"][0]["parameters"]["magnitude"] == 5.8
    assert listing["items"][0]["content_hash"]
    assert listing["items"][0]["change_reason"] == "事件建档初始参数"


def test_full_lifecycle_draft_published_revoked(client):
    event = _create_event(client)
    version_id = event["latest_parameter_version"]["id"]

    # 撤销原因必填
    missing = client.post(f"/api/seismic/parameter-versions/{version_id}/revoke", json={"reason": "", "operator": "x"})
    assert missing.status_code == 422

    # 草稿撤销：撤销后不应再有当前生效版本
    revoked = client.post(
        f"/api/seismic/parameter-versions/{version_id}/revoke",
        json={"reason": "参数来源不可靠", "operator": "reviewer-a"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["is_current"] is False
    assert revoked.json()["revoked_by"] == "reviewer-a"

    current = client.get(f"/api/seismic/events/{event['id']}/parameter-versions/current")
    assert current.status_code == 404

    # 已撤销版本不可再发布、不可再改
    republish = client.post(
        f"/api/seismic/parameter-versions/{version_id}/publish",
        json={"reason": "x", "operator": "y"},
    )
    assert republish.status_code == 409
    edit = client.patch(f"/api/seismic/parameter-versions/{version_id}", json={"magnitude": 6.0})
    assert edit.status_code == 409


def test_revision_publish_and_replay_distinguishes_states(client):
    event = _create_event(client, "EQ-VER-002")
    v1_id = event["latest_parameter_version"]["id"]
    published_v1 = _publish(client, v1_id, reason="首轮速报")
    assert published_v1["state"] == "published"
    assert published_v1["is_current"] is True
    assert published_v1["published_by"] == "duty-officer"

    current_event = client.get(f"/api/seismic/events/{event['id']}").json()
    assert current_event["magnitude"] == 5.8
    assert current_event["current_parameter_version"]["version"] == 1

    # 新增台站后修订：基于 v1 创建 v2 草稿
    created = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.1, "depth_km": 15.0, "reason": "新增 3 个台站重新定位", "base_version": 1, "created_by": "analyst-b"},
    )
    assert created.status_code == 201, created.text
    v2 = created.json()
    assert v2["version"] == 2
    assert v2["state"] == "draft"
    assert v2["parent_version"] == 1
    assert v2["is_current"] is False
    assert v2["created_by"] == "analyst-b"

    # v2 还是草稿，当前生效结论仍是 v1，值班读取不会被误导
    mid = client.get(f"/api/seismic/events/{event['id']}").json()
    assert mid["magnitude"] == 5.8
    assert mid["current_parameter_version"]["version"] == 1
    assert mid["latest_parameter_version"]["version"] == 2

    _publish(client, v2["id"], operator="analyst-b", reason="正式修订")

    # v2 生效：v1 自动变为 revoked，快照保留可回放
    after = client.get(f"/api/seismic/events/{event['id']}").json()
    assert after["magnitude"] == 6.1
    assert after["depth_km"] == 15.0
    assert after["current_parameter_version"]["version"] == 2

    items = client.get(f"/api/seismic/events/{event['id']}/parameter-versions").json()["items"]
    by_version = {item["version"]: item for item in items}
    assert by_version[2]["state"] == "published"
    assert by_version[2]["is_current"] is True
    assert by_version[1]["state"] == "revoked"
    assert by_version[1]["is_current"] is False
    assert by_version[1]["revoke_reason"] == "新版本发布自动失效"
    # 不可变快照内容未被覆盖
    assert by_version[1]["parameters"]["magnitude"] == 5.8

    # 按版本回放：?parameter_version=1 重现旧结论
    replay = client.get(f"/api/seismic/events/{event['id']}?parameter_version=1").json()
    assert replay["magnitude"] == 5.8
    assert replay["depth_km"] == 12.0
    assert replay["replayed_parameter_version"]["version"] == 1
    assert replay["replayed_parameter_version"]["state"] == "revoked"
    assert replay["current_parameter_version"]["version"] == 2

    replay_missing = client.get(f"/api/seismic/events/{event['id']}?parameter_version=99")
    assert replay_missing.status_code == 404


def test_stale_base_version_is_rejected(client):
    event = _create_event(client, "EQ-VER-003")
    v1_id = event["latest_parameter_version"]["id"]
    _publish(client, v1_id)

    v2 = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.1, "reason": "修订", "base_version": 1, "created_by": "a"},
    ).json()
    _publish(client, v2["id"], operator="a")

    # 另一名操作者仍基于 v1 提交：不能静默覆盖 v2
    stale = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 5.5, "reason": "迟到的旧基线提交", "base_version": 1, "created_by": "b"},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["context"]["submitted_base_version"] == 1
    assert detail["context"]["latest_version"] == 2
    # 新数据完好
    assert client.get(f"/api/seismic/events/{event['id']}").json()["magnitude"] == 6.1


def test_open_draft_blocks_parallel_revision(client):
    event = _create_event(client, "EQ-VER-004")
    _publish(client, event["latest_parameter_version"]["id"])
    first = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.0, "reason": "修订中", "base_version": 1, "created_by": "a"},
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.2, "reason": "并发修订", "base_version": 2, "created_by": "b"},
    )
    assert second.status_code == 409
    assert second.json()["detail"]["context"]["draft_version"] == 2


def test_if_match_detects_concurrent_draft_edit(client):
    event = _create_event(client, "EQ-VER-005")
    v1 = event["latest_parameter_version"]

    ok = client.patch(
        f"/api/seismic/parameter-versions/{v1['id']}",
        json={"magnitude": 6.0, "operator": "a"},
        headers={"If-Match": f'"{v1["content_hash"]}"'},
    )
    assert ok.status_code == 200, ok.text
    new_hash = ok.json()["content_hash"]
    assert new_hash != v1["content_hash"]

    # 拿着旧 hash 再提交必须失败，不能静默覆盖
    stale = client.patch(
        f"/api/seismic/parameter-versions/{v1['id']}",
        json={"magnitude": 6.3, "operator": "b"},
        headers={"If-Match": f'"{v1["content_hash"]}"'},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["context"]["actual_content_hash"] == new_hash

    # 用新 hash 可以继续
    again = client.patch(
        f"/api/seismic/parameter-versions/{v1['id']}",
        json={"magnitude": 6.3, "operator": "b"},
        headers={"If-Match": f'"{new_hash}"'},
    )
    assert again.status_code == 200


def test_published_and_revoked_versions_are_immutable(client):
    event = _create_event(client, "EQ-VER-006")
    v1_id = event["latest_parameter_version"]["id"]
    _publish(client, v1_id)

    edit_published = client.patch(f"/api/seismic/parameter-versions/{v1_id}", json={"magnitude": 9.0})
    assert edit_published.status_code == 409

    # 发布撤销后的版本同样冻结
    revoked = client.post(
        f"/api/seismic/parameter-versions/{v1_id}/revoke",
        json={"reason": "误报撤回", "operator": "chief"},
    )
    assert revoked.status_code == 200
    edit_revoked = client.patch(f"/api/seismic/parameter-versions/{v1_id}", json={"magnitude": 9.0})
    assert edit_revoked.status_code == 409


def test_computation_pins_input_parameter_version(client):
    event = _create_event(client, "EQ-VER-007", magnitude=5.8)
    v1_id = event["latest_parameter_version"]["id"]
    _publish(client, v1_id)

    observation = client.post(
        f"/api/seismic/events/{event['id']}/observations",
        json={"station_code": "SC01", "channel": "HNZ", "observed_at": "2026-09-24T12:00:03+00:00", "pga": 0.8, "distance_km": 18},
    )
    assert observation.status_code == 201

    enqueued = client.post(
        f"/api/seismic/events/{event['id']}/computations",
        json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20, "requested_by": "test"},
    )
    assert enqueued.status_code == 202
    task1 = enqueued.json()
    assert task1["parameter_version_info"]["version"] == 1
    assert task1["parameter_version_info"]["state"] == "published"
    assert task1["parameter_version_info"]["is_current"] is True

    # 发布修订版本：旧任务引用的 v1 失效，但引用关系不变
    v2 = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.4, "reason": "新增台站修订", "base_version": 1, "created_by": "a"},
    ).json()
    _publish(client, v2["id"], operator="a")

    pinned = client.get(f"/api/seismic/computations/{task1['id']}").json()
    assert pinned["parameter_version_info"]["version"] == 1
    assert pinned["parameter_version_info"]["state"] == "revoked"
    assert pinned["parameter_version_info"]["is_current"] is False

    claimed = client.post("/api/seismic/computations/claim?worker_id=w1").json()["task"]
    assert claimed["id"] == task1["id"]
    assert claimed["parameter_version_info"]["version"] == 1
    result = client.post(f"/api/seismic/computations/{task1['id']}/calculate?worker_id=w1")
    assert result.status_code == 200
    done = result.json()
    assert done["status"] == "done"
    # 结果明确引用输入版本
    import json

    result_body = json.loads(done["result_json"])
    assert result_body["parameter_version_info"]["version"] == 1
    assert result_body["input_digest"] == task1["input_digest"]

    # 修订后重新入队的任务引用 v2，摘要随输入版本变化
    second = client.post(
        f"/api/seismic/events/{event['id']}/computations",
        json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20, "requested_by": "test"},
    ).json()
    assert second["parameter_version_info"]["version"] == 2
    assert second["input_digest"] != task1["input_digest"]


def test_legacy_patch_autopublishes_snapshot_and_status_only_patch(client):
    event = _create_event(client, "EQ-VER-008")
    v1_id = event["latest_parameter_version"]["id"]
    _publish(client, v1_id)

    # 旧接口仍可用：直接修订参数会自动生成并发布新版本快照
    patched = client.patch(
        f"/api/seismic/events/{event['id']}",
        json={"magnitude": 6.2, "reason": "台网中心正式速报修订"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["magnitude"] == 6.2

    listing = client.get(f"/api/seismic/events/{event['id']}/parameter-versions").json()
    assert listing["current_version_id"] is not None
    versions = {item["version"]: item for item in listing["items"]}
    assert versions[2]["state"] == "published"
    assert versions[2]["parameters"]["magnitude"] == 6.2
    assert versions[2]["change_reason"] == "台网中心正式速报修订"
    assert versions[1]["state"] == "revoked"
    assert versions[1]["parameters"]["magnitude"] == 5.8

    # 仅改状态（不改参数）不产生新版本
    status_only = client.patch(f"/api/seismic/events/{event['id']}", json={"status": "review"})
    assert status_only.status_code == 200
    same = client.get(f"/api/seismic/events/{event['id']}/parameter-versions").json()
    assert len(same["items"]) == 2

    # 存在未发布草稿时，旧接口拒绝静默覆盖
    client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.5, "reason": "复核中", "base_version": 2, "created_by": "a"},
    )
    conflict = client.patch(f"/api/seismic/events/{event['id']}", json={"magnitude": 7.0})
    assert conflict.status_code == 409


def test_reason_required_for_draft_creation(client):
    event = _create_event(client, "EQ-VER-009")
    response = client.post(
        f"/api/seismic/events/{event['id']}/parameter-versions",
        json={"magnitude": 6.0, "base_version": 1, "created_by": "a"},
    )
    assert response.status_code == 422


def test_legacy_event_from_before_upgrade_is_backfilled(client):
    """升级前库中的事件（无参数版本）在 schema 初始化时补建 v1 已发布快照。"""
    connection = SeismicService().connection
    now = "2026-01-01T00:00:00+00:00"
    cursor = connection.execute(
        "INSERT INTO seismic_events(external_id,origin_time,latitude,longitude,depth_km,magnitude,"
        "magnitude_type,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("EQ-LEGACY-001", now, 31.0, 104.0, 20.0, 4.5, "MS", "legacy", now, now),
    )
    legacy_id = cursor.lastrowid

    from app.seismic.service import ensure_schema

    ensure_schema()

    service = SeismicService()
    listing = service.list_parameter_versions(legacy_id)
    assert listing["current_version_id"] is not None
    assert len(listing["items"]) == 1
    snapshot = listing["items"][0]
    assert snapshot["version"] == 1
    assert snapshot["state"] == "published"
    assert snapshot["is_current"] is True
    assert snapshot["parameters"] == {"depth_km": 20.0, "magnitude": 4.5, "magnitude_type": "MS"}
    assert snapshot["created_by"] == "system"
