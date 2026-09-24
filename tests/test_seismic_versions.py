from __future__ import annotations


def event_payload(external_id: str = "EQ-VER-001"):
    return {
        "external_id": external_id,
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": 5.8,
        "magnitude_type": "ML",
        "source": "test",
    }


def create_event(client, external_id="EQ-VER-001"):
    response = client.post("/api/seismic/events", json=event_payload(external_id))
    assert response.status_code == 201, response.text
    return response.json()


def test_event_creation_seeds_published_v1(client):
    event = create_event(client)
    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"] is not None
    current = detail["current_param_version"]
    assert current["version"] == 1
    assert current["status"] == "published"
    assert current["depth_km"] == 12.0
    assert current["magnitude"] == 5.8
    assert current["change_reason"] == "事件建档初始参数"
    assert current["actor"] == "test"

    versions = client.get(f"/api/seismic/events/{event['id']}/versions").json()
    assert versions["current_param_version"] == 1
    assert [item["status"] for item in versions["versions"]] == ["published"]


def test_draft_publish_supersedes_old_version(client):
    event = create_event(client, "EQ-VER-002")
    created = client.post(
        f"/api/seismic/events/{event['id']}/versions",
        json={"depth_km": 18.5, "magnitude": 6.1, "change_reason": "新增台站修订震级与深度", "actor": "duty-officer", "base_version": 1},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["version"] == 2
    assert draft["status"] == "draft"
    assert draft["base_version"] == 1

    # 草稿不影响当前生效版本，值班读取仍是 v1 结论。
    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"]["version"] == 1
    assert detail["depth_km"] == 12.0

    published = client.post(f"/api/seismic/events/{event['id']}/versions/2/publish", json={"actor": "duty-officer"}).json()
    assert published["status"] == "published"

    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"]["version"] == 2
    assert detail["depth_km"] == 18.5
    assert detail["magnitude"] == 6.1

    versions = client.get(f"/api/seismic/events/{event['id']}/versions").json()
    statuses = {item["version"]: item["status"] for item in versions["versions"]}
    assert statuses == {1: "revoked", 2: "published"}
    assert versions["versions"][0]["superseded_by_version"] == 2


def test_snapshot_is_immutable(client):
    event = create_event(client, "EQ-VER-003")
    client.post(
        f"/api/seismic/events/{event['id']}/versions",
        json={"magnitude": 6.4, "change_reason": "二次修订", "base_version": 1},
    )
    client.post(f"/api/seismic/events/{event['id']}/versions/2/publish")
    # v1 快照内容不随后续修订改变。
    v1 = client.get(f"/api/seismic/events/{event['id']}/versions/1").json()
    assert v1["magnitude"] == 5.8
    assert v1["depth_km"] == 12.0
    assert v1["status"] == "revoked"


def test_stale_base_version_conflict_is_rejected(client):
    event = create_event(client, "EQ-VER-004")
    first = client.post(
        f"/api/seismic/events/{event['id']}/versions",
        json={"magnitude": 6.0, "change_reason": "甲值班修订", "base_version": 1},
    )
    assert first.status_code == 201
    # 第二个请求仍基于 v1，而最新草稿已是 v2，必须报冲突而不是覆盖。
    conflict = client.post(
        f"/api/seismic/events/{event['id']}/versions",
        json={"magnitude": 5.5, "change_reason": "乙值班基于旧版修订", "base_version": 1},
    )
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["context"]["latest_version"] == 2
    assert body["error"]["context"]["base_version"] == 1

    # 基于最新版本重新提交可以成功。
    retry = client.post(
        f"/api/seismic/events/{event['id']}/versions",
        json={"magnitude": 5.5, "change_reason": "乙值班基于v2修订", "base_version": 2},
    )
    assert retry.status_code == 201
    assert retry.json()["version"] == 3


def test_legacy_patch_conflict_and_compatibility(client):
    event = create_event(client, "EQ-VER-005")
    client.post(f"/api/seismic/events/{event['id']}/versions", json={"magnitude": 6.2, "change_reason": "新草稿", "base_version": 1})
    conflict = client.patch(f"/api/seismic/events/{event['id']}", json={"magnitude": 5.0, "reason": "旧客户端补丁", "base_version": 1})
    assert conflict.status_code == 409

    # 不带 base_version 的旧客户端仍可工作（兼容）。
    ok = client.patch(f"/api/seismic/events/{event['id']}", json={"magnitude": 5.9, "reason": "旧客户端补丁"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["magnitude"] == 5.9
    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"]["version"] == 3


def test_replay_event_at_old_version(client):
    event = create_event(client, "EQ-VER-006")
    client.post(f"/api/seismic/events/{event['id']}/versions", json={"depth_km": 25.0, "magnitude": 6.3, "change_reason": "新增台站", "base_version": 1})
    client.post(f"/api/seismic/events/{event['id']}/versions/2/publish")

    replayed = client.get(f"/api/seismic/events/{event['id']}?at_version=1").json()
    assert replayed["replay"] is True
    assert replayed["depth_km"] == 12.0
    assert replayed["magnitude"] == 5.8
    assert replayed["replay_param_version"]["status"] == "revoked"
    # 当前生效版本信息仍然展示，避免误把旧结果当当前结论。
    assert replayed["current_param_version"]["version"] == 2

    missing = client.get(f"/api/seismic/events/{event['id']}?at_version=99")
    assert missing.status_code == 404


def test_revoke_current_version_restores_predecessor(client):
    event = create_event(client, "EQ-VER-007")
    client.post(f"/api/seismic/events/{event['id']}/versions", json={"magnitude": 6.3, "change_reason": "修订", "base_version": 1})
    client.post(f"/api/seismic/events/{event['id']}/versions/2/publish")
    revoked = client.post(f"/api/seismic/events/{event['id']}/versions/2/revoke", json={"reason": "参数存疑"}).json()
    assert revoked["status"] == "revoked"

    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"]["version"] == 1
    assert detail["current_param_version"]["status"] == "published"

    # v1 再次撤销后没有可恢复版本。
    client.post(f"/api/seismic/events/{event['id']}/versions/1/revoke")
    detail = client.get(f"/api/seismic/events/{event['id']}").json()
    assert detail["current_param_version"] is None
    again = client.post(f"/api/seismic/events/{event['id']}/versions/1/revoke")
    assert again.status_code == 409


def test_computation_references_param_version_and_can_replay(client):
    event = create_event(client, "EQ-VER-008")
    obs = client.post(
        f"/api/seismic/events/{event['id']}/observations",
        json={"station_code": "SC01", "channel": "HNZ", "observed_at": "2026-09-24T12:00:03+00:00", "pga": 0.8, "pgv": 2.1, "distance_km": 18},
    )
    assert obs.status_code == 201

    task_v1 = client.post(f"/api/seismic/events/{event['id']}/computations", json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20}).json()
    assert task_v1["param_version"] == 1
    assert task_v1["param_status"] == "published"

    # 发布 v2 后，新计算引用 v2。
    client.post(f"/api/seismic/events/{event['id']}/versions", json={"magnitude": 7.0, "change_reason": "强震修订", "base_version": 1})
    client.post(f"/api/seismic/events/{event['id']}/versions/2/publish")
    task_v2 = client.post(f"/api/seismic/events/{event['id']}/computations", json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20}).json()
    assert task_v2["param_version"] == 2

    # 显式按 v1 回放计算。
    replay = client.post(
        f"/api/seismic/events/{event['id']}/computations",
        json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20, "param_version": 1},
    ).json()
    assert replay["param_version"] == 1
    assert replay["id"] == task_v1["id"]  # 相同输入摘要去重

    # 完成任务，结果中明确引用输入参数版本与快照。
    for expected_version, task_id in ((1, task_v1["id"]), (2, task_v2["id"])):
        claimed = client.post("/api/seismic/computations/claim?worker_id=w1").json()["task"]
        assert claimed["id"] == task_id
        done = client.post(f"/api/seismic/computations/{task_id}/calculate?worker_id=w1").json()
        assert done["status"] == "done"
        import json
        result = json.loads(done["result_json"])
        assert result["param_version"] == expected_version
        assert result["param_snapshot"]["magnitude"] == (5.8 if expected_version == 1 else 7.0)


def test_computation_on_draft_version_allowed_for_review(client):
    event = create_event(client, "EQ-VER-009")
    client.post(f"/api/seismic/events/{event['id']}/versions", json={"magnitude": 6.9, "change_reason": "待评审草稿", "base_version": 1})
    replay = client.post(
        f"/api/seismic/events/{event['id']}/computations",
        json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20, "param_version": 2},
    ).json()
    assert replay["param_version"] == 2
    assert replay["param_status"] == "draft"


def test_concurrent_stale_updates_do_not_silently_overwrite(client):
    import threading

    event = create_event(client, "EQ-VER-010")
    results: list[int] = []
    barrier = threading.Barrier(4)

    def submit(magnitude: float) -> None:
        barrier.wait()
        response = client.post(
            f"/api/seismic/events/{event['id']}/versions",
            json={"magnitude": magnitude, "change_reason": f"并发修订 {magnitude}", "base_version": 1},
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=submit, args=(6.0 + index * 0.1,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 恰好一个提交成功，其余全部冲突，没有静默覆盖。
    assert sorted(results) == [201, 409, 409, 409]
    versions = client.get(f"/api/seismic/events/{event['id']}/versions").json()
    assert len(versions["versions"]) == 2  # v1 + 唯一一个 v2
