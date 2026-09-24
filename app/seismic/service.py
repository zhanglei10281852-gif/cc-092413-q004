from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    origin_time TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    depth_km REAL NOT NULL,
    magnitude REAL NOT NULL,
    magnitude_type TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','review','published','archived')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES seismic_events(id) ON DELETE RESTRICT,
    station_code TEXT NOT NULL,
    channel TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    pga REAL,
    pgv REAL,
    distance_km REAL NOT NULL,
    quality_score REAL NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL DEFAULT 'pending',
    quality_reason TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, station_code, channel, observed_at)
);
CREATE TABLE IF NOT EXISTS seismic_computations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES seismic_events(id) ON DELETE RESTRICT,
    task_key TEXT NOT NULL UNIQUE,
    model_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    grid_step_km REAL NOT NULL,
    radius_km REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','retry','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_parameter_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES seismic_events(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','revoked')),
    parameters_json TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'system',
    parent_version INTEGER,
    content_hash TEXT NOT NULL,
    published_by TEXT NOT NULL DEFAULT '',
    publish_reason TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    revoked_by TEXT NOT NULL DEFAULT '',
    revoke_reason TEXT NOT NULL DEFAULT '',
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, version)
);
CREATE TABLE IF NOT EXISTS seismic_event_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seismic_obs_event ON seismic_observations(event_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_seismic_tasks_status ON seismic_computations(status, created_at);
CREATE INDEX IF NOT EXISTS idx_seismic_paramver_event ON seismic_parameter_versions(event_id, version);
"""

# 每个事件至多有一个已发布版本（当前生效版本）
PUBLISHED_PARTIAL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_seismic_paramver_one_published "
    "ON seismic_parameter_versions(event_id) WHERE state='published'"
)

# 计算任务引用参数版本（历史库升级时补齐列）
_COMPUTATION_VERSION_COLUMNS = {
    "parameter_version_id": "INTEGER",
    "parameter_version": "INTEGER",
}

VERSIONED_PARAMS = ("depth_km", "magnitude", "magnitude_type")
PARAMETER_STATES = ("draft", "published", "revoked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    connection.execute(PUBLISHED_PARTIAL_INDEX)
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(seismic_computations)").fetchall()}
    for column, declaration in _COMPUTATION_VERSION_COLUMNS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE seismic_computations ADD COLUMN {column} {declaration}")
    _backfill_parameter_versions(connection)


def _backfill_parameter_versions(connection: sqlite3.Connection) -> None:
    """为升级前已存在的事件补建 v1 不可变快照，历史计算任务的版本引用保持为空。"""
    events = connection.execute(
        "SELECT e.* FROM seismic_events e LEFT JOIN seismic_parameter_versions v ON v.event_id=e.id "
        "WHERE v.id IS NULL"
    ).fetchall()
    for event in events:
        parameters = {key: event[key] for key in VERSIONED_PARAMS}
        now = _now()
        connection.execute(
            "INSERT INTO seismic_parameter_versions(event_id,version,state,parameters_json,change_reason,"
            "created_by,parent_version,content_hash,published_by,publish_reason,published_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["id"],
                1,
                "published",
                json.dumps(parameters, ensure_ascii=False),
                "历史数据迁移初始快照",
                "system",
                None,
                _content_hash(parameters),
                "system",
                "历史数据迁移",
                now,
                now,
                now,
            ),
        )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _content_hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _event_digest(event: sqlite3.Row, parameters: dict[str, Any], observations: list[sqlite3.Row]) -> str:
    payload = {
        "event": {
            "external_id": event["external_id"],
            "origin_time": event["origin_time"],
            "latitude": event["latitude"],
            "longitude": event["longitude"],
        },
        "parameters": parameters,
        "observations": [dict(item) for item in observations],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _quality(observation: dict[str, Any]) -> tuple[float, str, str]:
    reasons: list[str] = []
    score = 1.0
    if observation.get("pga") is None and observation.get("pgv") is None:
        score = 0.0
        reasons.append("缺少峰值指标")
    if observation.get("pga") is not None and observation["pga"] > 20:
        score -= 0.6
        reasons.append("PGA 超出量程")
    if observation.get("pgv") is not None and observation["pgv"] > 300:
        score -= 0.4
        reasons.append("PGV 超出量程")
    if observation.get("distance_km", 0) == 0:
        score -= 0.2
        reasons.append("距离为零")
    score = max(0.0, min(1.0, round(score, 3)))
    status = "accepted" if score >= 0.6 else "rejected"
    return score, status, "、".join(reasons) if reasons else "通过基础质量检查"


@dataclass(frozen=True)
class GridPoint:
    latitude: float
    longitude: float
    intensity: float


class SeismicService:
    """事件、观测和计算任务的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 序列化

    @staticmethod
    def _serialize_version(row: sqlite3.Row, *, is_current: bool | None = None) -> dict[str, Any]:
        data = dict(row)
        data["parameters"] = json.loads(data.pop("parameters_json"))
        if is_current is None:
            is_current = data["state"] == "published"
        data["is_current"] = bool(is_current)
        return data

    @staticmethod
    def _version_ref(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"id": row["id"], "version": row["version"], "state": row["state"], "content_hash": row["content_hash"]}

    def _attach_version_info(self, connection: sqlite3.Connection, result: dict[str, Any], event_id: int) -> None:
        latest = connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        current = connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND state='published'",
            (event_id,),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM seismic_parameter_versions WHERE event_id=?", (event_id,)
        ).fetchone()["n"]
        result["latest_parameter_version"] = self._version_ref(latest)
        result["current_parameter_version"] = self._version_ref(current)
        result["parameter_versions_count"] = count

    # ------------------------------------------------------------------ 事件

    def create_event(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO seismic_events(external_id,origin_time,latitude,longitude,depth_km,magnitude,magnitude_type,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["external_id"], payload["origin_time"], payload["latitude"], payload["longitude"], payload["depth_km"], payload["magnitude"], payload["magnitude_type"], payload["source"], now, now),
            )
            event_id = cursor.lastrowid
            connection.execute("INSERT INTO seismic_event_audit(event_id,action,actor,after_json,created_at) VALUES(?,?,?,?,?)", (event_id, "create", actor, json.dumps(payload, ensure_ascii=False), now))
            # 建档即生成 v1 草稿快照：首个参数版本不可缺失，便于后续按版本回放
            version_row = self._insert_version(
                connection,
                event_id=event_id,
                version_number=1,
                state="draft",
                parameters={key: payload[key] for key in VERSIONED_PARAMS},
                change_reason="事件建档初始参数",
                created_by=actor,
                parent_version=None,
                now=now,
            )
            event = _row(connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()) or {}
            event["latest_parameter_version"] = self._version_ref(version_row)
            event["current_parameter_version"] = None
            event["parameter_versions_count"] = 1
            return event

    def get_event(
        self,
        event_id: int,
        include_observations: bool = True,
        parameter_version: int | None = None,
    ) -> dict[str, Any] | None:
        event = self.connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return None
        result = _row(event) or {}
        self._attach_version_info(self.connection, result, event_id)
        replay_row: sqlite3.Row | None = None
        if parameter_version is not None:
            replay_row = self.connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND version=?",
                (event_id, parameter_version),
            ).fetchone()
            if replay_row is None:
                raise NotFoundError(
                    f"参数版本 {parameter_version} 不存在",
                    context={"event_id": event_id, "parameter_version": parameter_version},
                )
            snapshot = json.loads(replay_row["parameters_json"])
            result.update({key: snapshot[key] for key in VERSIONED_PARAMS})
            result["replayed_parameter_version"] = self._version_ref(replay_row)
        if include_observations:
            rows = self.connection.execute("SELECT * FROM seismic_observations WHERE event_id=? ORDER BY observed_at, id", (event_id,)).fetchall()
            result["observations"] = [dict(item) for item in rows]
        return result

    def patch_event(self, event_id: int, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        """兼容旧接口：直接修改参数会立即生成并发布一个不可变快照版本。"""
        with transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
            if current is None:
                raise KeyError("event_not_found")
            status = payload.get("status")
            parameter_values = {
                key: payload[key] for key in VERSIONED_PARAMS if payload.get(key) is not None
            }
            now = _now()
            if not parameter_values:
                if status is not None and status != current["status"]:
                    connection.execute(
                        "UPDATE seismic_events SET status=?, updated_at=? WHERE id=?",
                        (status, now, event_id),
                    )
                    connection.execute(
                        "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                        (event_id, "patch:status", actor, json.dumps({"status": current["status"]}, ensure_ascii=False), json.dumps({"status": status}, ensure_ascii=False), now),
                    )
                return dict(connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone())
            open_draft = connection.execute(
                "SELECT id FROM seismic_parameter_versions WHERE event_id=? AND state='draft'",
                (event_id,),
            ).fetchone()
            if open_draft is not None:
                raise ConflictError(
                    "事件存在未发布的草稿版本，不能通过兼容接口直接覆盖，请先发布或撤销草稿",
                    context={"event_id": event_id, "draft_version_id": open_draft["id"]},
                )
            latest = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            parameters = {key: current[key] for key in VERSIONED_PARAMS}
            parameters.update(parameter_values)
            reason = payload.get("reason") or "兼容接口直接修订参数"
            # 先把旧生效版本置为撤销，再插入新的已发布快照，避免“每事件唯一发布版本”冲突
            self._supersede_published(connection, event_id, now=now)
            version_row = self._insert_version(
                connection,
                event_id=event_id,
                version_number=(latest["version"] + 1 if latest else 1),
                state="published",
                parameters=parameters,
                change_reason=reason,
                created_by=actor,
                parent_version=(latest["version"] if latest else None),
                now=now,
                published_by=actor,
                publish_reason="兼容接口直接修订，自动发布",
            )
            assignments = ", ".join(f"{key}=?" for key in VERSIONED_PARAMS)
            connection.execute(
                f"UPDATE seismic_events SET {assignments}, version=?, status='published', updated_at=? WHERE id=?",
                (*(parameters[key] for key in VERSIONED_PARAMS), version_row["version"], now, event_id),
            )
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, "patch:publish", actor, json.dumps({key: current[key] for key in VERSIONED_PARAMS}, ensure_ascii=False), json.dumps(parameters, ensure_ascii=False), now),
            )
            return dict(connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone())

    # ---------------------------------------------------------- 参数版本管理

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        *,
        event_id: int,
        version_number: int,
        state: str,
        parameters: dict[str, Any],
        change_reason: str,
        created_by: str,
        parent_version: int | None,
        now: str,
        published_by: str = "",
        publish_reason: str = "",
        published_at: str | None = None,
    ) -> sqlite3.Row:
        if state == "published" and published_at is None:
            published_at = now
        cursor = connection.execute(
            "INSERT INTO seismic_parameter_versions(event_id,version,state,parameters_json,change_reason,"
            "created_by,parent_version,content_hash,published_by,publish_reason,published_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                version_number,
                state,
                json.dumps(parameters, ensure_ascii=False),
                change_reason,
                created_by,
                parent_version,
                _content_hash(parameters),
                published_by,
                publish_reason,
                published_at,
                now,
                now,
            ),
        )
        return connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE id=?", (cursor.lastrowid,)
        ).fetchone()

    def list_parameter_versions(self, event_id: int) -> dict[str, Any]:
        event = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("事件不存在", context={"event_id": event_id})
        rows = self.connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? ORDER BY version DESC",
            (event_id,),
        ).fetchall()
        current = self.connection.execute(
            "SELECT id FROM seismic_parameter_versions WHERE event_id=? AND state='published'",
            (event_id,),
        ).fetchone()
        current_id = current["id"] if current else None
        return {
            "event_id": event_id,
            "current_version_id": current_id,
            "items": [self._serialize_version(row, is_current=row["id"] == current_id) for row in rows],
        }

    def get_parameter_version(self, event_id: int, version_number: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND version=?",
            (event_id, version_number),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"参数版本 {version_number} 不存在",
                context={"event_id": event_id, "parameter_version": version_number},
            )
        return self._serialize_version(row)

    def get_current_parameter_version(self, event_id: int) -> dict[str, Any]:
        event = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("事件不存在", context={"event_id": event_id})
        row = self.connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND state='published'",
            (event_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "事件当前没有已发布的生效参数版本",
                context={"event_id": event_id},
            )
        return self._serialize_version(row, is_current=True)

    def _load_version_for_update(
        self, connection: sqlite3.Connection, version_id: int
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        row = connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("参数版本不存在", context={"parameter_version_id": version_id})
        event = connection.execute(
            "SELECT * FROM seismic_events WHERE id=?", (row["event_id"],)
        ).fetchone()
        return row, event

    def create_parameter_version(
        self,
        event_id: int,
        payload: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, Any]:
        """基于最新版本创建新的草稿快照；base_version 过期时拒绝，避免并发静默覆盖。"""
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationError("创建参数版本必须提供变更原因", context={"field": "reason"})
        with transaction(immediate=True) as connection:
            event = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                raise NotFoundError("事件不存在", context={"event_id": event_id})
            latest = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            base_version = payload.get("base_version")
            if base_version is not None and latest is not None and int(base_version) != latest["version"]:
                raise ConflictError(
                    "参数版本基线已过期，请基于最新版本重新提交",
                    context={
                        "event_id": event_id,
                        "submitted_base_version": base_version,
                        "latest_version": latest["version"],
                    },
                )
            open_draft = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND state='draft'",
                (event_id,),
            ).fetchone()
            if open_draft is not None:
                raise ConflictError(
                    "事件已存在未发布的草稿版本，不能重复创建",
                    context={"event_id": event_id, "draft_version": open_draft["version"]},
                )
            base_parameters = json.loads(latest["parameters_json"]) if latest else {key: event[key] for key in VERSIONED_PARAMS}
            parameters = dict(base_parameters)
            for key in VERSIONED_PARAMS:
                if payload.get(key) is not None:
                    parameters[key] = payload[key]
            now = _now()
            version_row = self._insert_version(
                connection,
                event_id=event_id,
                version_number=(latest["version"] + 1 if latest else 1),
                state="draft",
                parameters=parameters,
                change_reason=reason,
                created_by=actor,
                parent_version=(latest["version"] if latest else None),
                now=now,
            )
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    event_id,
                    "parameter_version.create_draft",
                    actor,
                    json.dumps(self._serialize_version(latest) if latest else {}, ensure_ascii=False),
                    json.dumps(self._serialize_version(version_row), ensure_ascii=False),
                    now,
                ),
            )
            return self._serialize_version(version_row, is_current=False)

    def update_draft_parameter_version(
        self,
        version_id: int,
        payload: dict[str, Any],
        actor: str = "system",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        """草稿在发布前可以继续修订；一旦发布或撤销即冻结。"""
        with transaction(immediate=True) as connection:
            row, _ = self._load_version_for_update(connection, version_id)
            if row["state"] != "draft":
                raise ConflictError(
                    "只有草稿版本可以修改，已发布和已撤销版本不可变",
                    context={"parameter_version_id": version_id, "state": row["state"]},
                )
            self._check_hash(row, expected_hash)
            parameters = json.loads(row["parameters_json"])
            changed = False
            for key in VERSIONED_PARAMS:
                if payload.get(key) is not None:
                    parameters[key] = payload[key]
                    changed = True
            reason = payload.get("reason")
            if reason is not None and reason.strip() and reason != row["change_reason"]:
                changed = True
            if not changed:
                return self._serialize_version(row, is_current=False)
            now = _now()
            connection.execute(
                "UPDATE seismic_parameter_versions SET parameters_json=?, change_reason=?, content_hash=?, updated_at=? WHERE id=?",
                (
                    json.dumps(parameters, ensure_ascii=False),
                    reason.strip() if reason is not None and reason.strip() else row["change_reason"],
                    _content_hash(parameters),
                    now,
                    version_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE id=?", (version_id,)
            ).fetchone()
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["event_id"],
                    "parameter_version.update_draft",
                    actor,
                    json.dumps(self._serialize_version(row), ensure_ascii=False),
                    json.dumps(self._serialize_version(updated), ensure_ascii=False),
                    now,
                ),
            )
            return self._serialize_version(updated, is_current=False)

    @staticmethod
    def _check_hash(row: sqlite3.Row, expected_hash: str | None) -> None:
        if expected_hash and expected_hash != row["content_hash"]:
            raise ConflictError(
                "参数版本内容已被其他人修改，请刷新后重试",
                context={
                    "parameter_version_id": row["id"],
                    "expected_content_hash": expected_hash,
                    "actual_content_hash": row["content_hash"],
                },
            )

    def _supersede_published(
        self,
        connection: sqlite3.Connection,
        event_id: int,
        *,
        keep_id: int | None = None,
        now: str,
    ) -> None:
        """发布新版本时，把旧的生效版本自动置为已撤销（保留快照，不删除）。"""
        if keep_id is None:
            connection.execute(
                "UPDATE seismic_parameter_versions SET state='revoked', revoked_by='system', "
                "revoke_reason='新版本发布自动失效', revoked_at=?, updated_at=? "
                "WHERE event_id=? AND state='published'",
                (now, now, event_id),
            )
        else:
            connection.execute(
                "UPDATE seismic_parameter_versions SET state='revoked', revoked_by='system', "
                "revoke_reason='新版本发布自动失效', revoked_at=?, updated_at=? "
                "WHERE event_id=? AND state='published' AND id<>?",
                (now, now, event_id, keep_id),
            )

    def publish_parameter_version(
        self,
        version_id: int,
        actor: str = "system",
        reason: str = "",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row, event = self._load_version_for_update(connection, version_id)
            if row["state"] == "published":
                return self._serialize_version(row, is_current=True)
            if row["state"] == "revoked":
                raise ConflictError(
                    "已撤销的参数版本不能重新发布，请基于当前版本新建草稿",
                    context={"parameter_version_id": version_id, "version": row["version"]},
                )
            self._check_hash(row, expected_hash)
            now = _now()
            parameters = json.loads(row["parameters_json"])
            # 目标版本当前是草稿，先撤销旧生效版本再把草稿置为发布
            self._supersede_published(connection, row["event_id"], now=now)
            connection.execute(
                "UPDATE seismic_parameter_versions SET state='published', published_by=?, publish_reason=?, "
                "published_at=?, updated_at=? WHERE id=?",
                (actor, reason or "正式发布", now, now, version_id),
            )
            assignments = ", ".join(f"{key}=?" for key in VERSIONED_PARAMS)
            new_status = "archived" if event["status"] == "archived" else "published"
            connection.execute(
                f"UPDATE seismic_events SET {assignments}, version=?, status=?, updated_at=? WHERE id=?",
                (*(parameters[key] for key in VERSIONED_PARAMS), row["version"], new_status, now, event["id"]),
            )
            published = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE id=?", (version_id,)
            ).fetchone()
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    event["id"],
                    "parameter_version.publish",
                    actor,
                    json.dumps(self._serialize_version(row), ensure_ascii=False),
                    json.dumps(self._serialize_version(published), ensure_ascii=False),
                    now,
                ),
            )
            return self._serialize_version(published, is_current=True)

    def revoke_parameter_version(
        self,
        version_id: int,
        actor: str = "system",
        reason: str = "",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("撤销参数版本必须提供撤销原因", context={"field": "reason"})
        with transaction(immediate=True) as connection:
            row, event = self._load_version_for_update(connection, version_id)
            if row["state"] == "revoked":
                raise ConflictError(
                    "参数版本已经撤销，不能重复撤销",
                    context={"parameter_version_id": version_id, "state": row["state"]},
                )
            self._check_hash(row, expected_hash)
            now = _now()
            if row["state"] == "draft":
                # 草稿尚未生效，撤销等同于废弃该草稿，不影响事件当前结论
                action = "parameter_version.discard_draft"
            else:
                action = "parameter_version.revoke"
            connection.execute(
                "UPDATE seismic_parameter_versions SET state='revoked', revoked_by=?, revoke_reason=?, "
                "revoked_at=?, updated_at=? WHERE id=?",
                (actor, reason, now, now, version_id),
            )
            connection.execute(
                "UPDATE seismic_events SET updated_at=? WHERE id=?", (now, event["id"])
            )
            revoked = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE id=?", (version_id,)
            ).fetchone()
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    event["id"],
                    action,
                    actor,
                    json.dumps(self._serialize_version(row), ensure_ascii=False),
                    json.dumps(self._serialize_version(revoked), ensure_ascii=False),
                    now,
                ),
            )
            return self._serialize_version(revoked, is_current=False)

    # ------------------------------------------------------------------ 观测

    def add_observation(self, event_id: int, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        event = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise KeyError("event_not_found")
        quality_score, quality_status, quality_reason = _quality(payload)
        now = _now()
        source_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO seismic_observations(event_id,station_code,channel,observed_at,pga,pgv,distance_km,quality_score,quality_status,quality_reason,source_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, payload["station_code"], payload["channel"], payload["observed_at"], payload.get("pga"), payload.get("pgv"), payload["distance_km"], quality_score, quality_status, quality_reason, source_hash, now),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute("SELECT * FROM seismic_observations WHERE event_id=? AND station_code=? AND channel=? AND observed_at=?", (event_id, payload["station_code"], payload["channel"], payload["observed_at"])).fetchone()
                return dict(existing) if existing else {}
            return dict(connection.execute("SELECT * FROM seismic_observations WHERE id=?", (cursor.lastrowid,)).fetchone())

    # ------------------------------------------------------------------ 计算

    def _grid(
        self,
        latitude: float,
        longitude: float,
        magnitude: float,
        observations: list[sqlite3.Row],
        step: float,
        radius: float,
    ) -> list[GridPoint]:
        center_lat, center_lon = float(latitude), float(longitude)
        radius_deg = radius / 111.0
        count = max(1, int(math.floor((radius * 2) / step)))
        result: list[GridPoint] = []
        accepted = [item for item in observations if item["quality_status"] == "accepted"]
        for lat_index in range(count + 1):
            lat = center_lat - radius_deg + lat_index * (step / 111.0)
            for lon_index in range(count + 1):
                lon = center_lon - radius_deg + lon_index * (step / 111.0) / max(0.2, math.cos(math.radians(lat)))
                values = []
                for item in accepted:
                    distance = math.hypot((lat - center_lat) * 111, (lon - center_lon) * 111 * max(0.2, math.cos(math.radians(lat))))
                    weight = 1 / max(1, abs(distance - float(item["distance_km"])))
                    estimate = magnitude - math.log10(max(1, float(item["distance_km"]))) + (float(item["pga"] or 0) * 0.01)
                    values.append((estimate * weight, weight))
                intensity = round(sum(value for value, _ in values) / sum(weight for _, weight in values), 3) if values else round(magnitude - 1, 3)
                result.append(GridPoint(round(lat, 6), round(lon, 6), intensity))
        return result

    def _resolve_computation_version(
        self, connection: sqlite3.Connection, event_id: int, parameter_version_id: int | None
    ) -> sqlite3.Row:
        if parameter_version_id is not None:
            version = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE id=? AND event_id=?",
                (parameter_version_id, event_id),
            ).fetchone()
            if version is None:
                raise NotFoundError(
                    "指定的参数版本不存在或不属于该事件",
                    context={"event_id": event_id, "parameter_version_id": parameter_version_id},
                )
            return version
        version = connection.execute(
            "SELECT * FROM seismic_parameter_versions WHERE event_id=? AND state='published'",
            (event_id,),
        ).fetchone()
        if version is None:
            version = connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        if version is None:
            raise ValidationError("事件尚无参数版本，无法计算", context={"event_id": event_id})
        return version

    def enqueue_computation(
        self,
        event_id: int,
        model_version: str,
        grid_step_km: float,
        radius_km: float,
        requested_by: str,
        parameter_version_id: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            event = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                raise KeyError("event_not_found")
            version = self._resolve_computation_version(connection, event_id, parameter_version_id)
            parameters = json.loads(version["parameters_json"])
            observations = connection.execute(
                "SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (event_id,)
            ).fetchall()
            digest = _event_digest(event, parameters, observations)
            task_key = hashlib.sha256(
                f"{event_id}:{version['id']}:{digest}:{model_version}:{grid_step_km}:{radius_km}".encode()
            ).hexdigest()
            existing = connection.execute("SELECT * FROM seismic_computations WHERE task_key=?", (task_key,)).fetchone()
            if existing:
                return self._attach_task_version(connection, dict(existing))
            cursor = connection.execute(
                "INSERT INTO seismic_computations(event_id,task_key,model_version,input_digest,grid_step_km,radius_km,"
                "parameter_version_id,parameter_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, task_key, model_version, digest, grid_step_km, radius_km, version["id"], version["version"], now, now),
            )
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,after_json,created_at) VALUES(?,?,?,?,?)",
                (
                    event_id,
                    "compute.enqueue",
                    requested_by,
                    json.dumps(
                        {
                            "task_key": task_key,
                            "model_version": model_version,
                            "parameter_version_id": version["id"],
                            "parameter_version": version["version"],
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            task = connection.execute("SELECT * FROM seismic_computations WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._attach_task_version(connection, dict(task))

    def _attach_task_version(self, connection: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
        version_id = task.get("parameter_version_id")
        if version_id is None:
            task["parameter_version_info"] = None
            return task
        row = connection.execute(
            "SELECT id,version,state,content_hash,revoked_at FROM seismic_parameter_versions WHERE id=?",
            (version_id,),
        ).fetchone()
        task["parameter_version_info"] = None if row is None else {
            "id": row["id"],
            "version": row["version"],
            "state": row["state"],
            "content_hash": row["content_hash"],
            "is_current": row["state"] == "published",
            "revoked_at": row["revoked_at"],
        }
        return task

    def claim_task(self, worker_id: str) -> dict[str, Any] | None:
        now = _now()
        with transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM seismic_computations WHERE status IN ('queued','retry') ORDER BY created_at,id LIMIT 1").fetchone()
            if task is None:
                return None
            connection.execute("UPDATE seismic_computations SET status='leased', attempts=attempts+1, lease_owner=?, lease_until=?, updated_at=? WHERE id=?", (worker_id, now, now, task["id"]))
            row = connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task["id"],)).fetchone()
            return self._attach_task_version(connection, dict(row))

    def complete_task(self, task_id: int, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
            if task is None or task["status"] != "leased" or task["lease_owner"] != worker_id:
                raise KeyError("task_not_owned")
            now = _now()
            connection.execute("UPDATE seismic_computations SET status='done',result_json=?,lease_owner='',lease_until='',updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), now, task_id))
            row = connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
            return self._attach_task_version(connection, dict(row))

    def calculate_task(self, task_id: int, worker_id: str) -> dict[str, Any]:
        task = self.connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
        if task is None or task["status"] != "leased" or task["lease_owner"] != worker_id:
            raise KeyError("task_not_owned")
        event = self.connection.execute("SELECT * FROM seismic_events WHERE id=?", (task["event_id"],)).fetchone()
        observations = self.connection.execute("SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (task["event_id"],)).fetchall()
        # 始终使用任务引用的版本快照，发布新版本或撤销旧版本都不会改变既有结果
        if task["parameter_version_id"] is not None:
            version = self.connection.execute(
                "SELECT * FROM seismic_parameter_versions WHERE id=?", (task["parameter_version_id"],)
            ).fetchone()
            if version is None:
                raise KeyError("parameter_version_missing")
            parameters = json.loads(version["parameters_json"])
            version_ref = self._version_ref(version)
        else:
            parameters = {key: event[key] for key in VERSIONED_PARAMS}
            version_ref = None
        points = self._grid(
            event["latitude"], event["longitude"], float(parameters["magnitude"]),
            observations, task["grid_step_km"], task["radius_km"],
        )
        result = {
            "model_version": task["model_version"],
            "input_digest": task["input_digest"],
            "parameter_version_info": version_ref,
            "points": [point.__dict__ for point in points],
            "count": len(points),
        }
        return self.complete_task(task_id, worker_id, result)

    def get_computation(self, task_id: int) -> dict[str, Any] | None:
        task = self.connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return None
        return self._attach_task_version(self.connection, dict(task))
