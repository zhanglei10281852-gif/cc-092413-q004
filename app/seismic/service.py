from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError
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
    param_version INTEGER NOT NULL DEFAULT 1,
    param_status TEXT NOT NULL DEFAULT 'published',
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
CREATE TABLE IF NOT EXISTS seismic_event_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES seismic_events(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    depth_km REAL NOT NULL,
    magnitude REAL NOT NULL,
    magnitude_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','revoked')),
    change_reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    base_version INTEGER,
    superseded_by_version INTEGER,
    created_at TEXT NOT NULL,
    published_at TEXT,
    revoked_at TEXT,
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
CREATE INDEX IF NOT EXISTS idx_seismic_versions_event ON seismic_event_versions(event_id, version);
"""

# 旧库补列：参数版本功能上线前已存在的 seismic_computations 表。
_COMPUTATION_MIGRATIONS = {
    "param_version": "ALTER TABLE seismic_computations ADD COLUMN param_version INTEGER NOT NULL DEFAULT 1",
    "param_status": "ALTER TABLE seismic_computations ADD COLUMN param_status TEXT NOT NULL DEFAULT 'published'",
}

PARAM_FIELDS = ("depth_km", "magnitude", "magnitude_type")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(seismic_computations)").fetchall()}
    for name, statement in _COMPUTATION_MIGRATIONS.items():
        if name not in columns:
            connection.execute(statement)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _event_digest(event: sqlite3.Row | dict[str, Any], observations: list[sqlite3.Row], version: sqlite3.Row | dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "event": {key: value for key, value in dict(event).items() if key not in {"version", "updated_at"}},
        "observations": [dict(item) for item in observations],
    }
    if version is not None:
        # 只纳入不可变快照字段；发布/撤销等生命周期变化不改变计算输入身份。
        payload["param_version"] = {key: dict(version)[key] for key in ("version", "depth_km", "magnitude", "magnitude_type")}
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
    """事件、参数版本、观测和计算任务的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 版本

    @staticmethod
    def _head_version(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? ORDER BY version DESC LIMIT 1",
            (event_id,),
        ).fetchone()

    @staticmethod
    def _current_version(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? AND status='published' ORDER BY version DESC LIMIT 1",
            (event_id,),
        ).fetchone()

    def _require_event(self, connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        event = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise KeyError("event_not_found")
        return event

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        event_id: int,
        values: dict[str, Any],
        actor: str,
        reason: str,
        base_version: int | None,
        now: str,
        status: str = "draft",
    ) -> dict[str, Any]:
        head = self._head_version(connection, event_id)
        next_version = (head["version"] + 1) if head else 1
        if base_version is None:
            base_version = head["version"] if head else None
        cursor = connection.execute(
            "INSERT INTO seismic_event_versions(event_id,version,depth_km,magnitude,magnitude_type,status,change_reason,actor,base_version,created_at,published_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                next_version,
                values["depth_km"],
                values["magnitude"],
                values["magnitude_type"],
                status,
                reason,
                actor,
                base_version,
                now,
                now if status == "published" else None,
            ),
        )
        return dict(connection.execute("SELECT * FROM seismic_event_versions WHERE id=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _publish_locked(connection: sqlite3.Connection, event_id: int, version_row: dict[str, Any], now: str) -> dict[str, Any]:
        """把指定版本置为生效：旧生效版本撤销，事件主表同步为快照值。"""
        connection.execute(
            "UPDATE seismic_event_versions SET status='revoked', superseded_by_version=?, revoked_at=? "
            "WHERE event_id=? AND status='published'",
            (version_row["version"], now, event_id),
        )
        connection.execute(
            "UPDATE seismic_event_versions SET status='published', published_at=COALESCE(published_at,?) WHERE event_id=? AND version=?",
            (now, event_id, version_row["version"]),
        )
        connection.execute(
            "UPDATE seismic_events SET depth_km=?,magnitude=?,magnitude_type=?,version=?,updated_at=? WHERE id=?",
            (version_row["depth_km"], version_row["magnitude"], version_row["magnitude_type"], version_row["version"], now, event_id),
        )
        return dict(connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
            (event_id, version_row["version"]),
        ).fetchone())

    def create_event(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO seismic_events(external_id,origin_time,latitude,longitude,depth_km,magnitude,magnitude_type,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["external_id"], payload["origin_time"], payload["latitude"], payload["longitude"], payload["depth_km"], payload["magnitude"], payload["magnitude_type"], payload["source"], now, now),
            )
            event_id = cursor.lastrowid
            # 建档即固化 v1 已发布参数版本，后续计算始终引用不可变快照。
            self._insert_version(connection, event_id, payload, actor, "事件建档初始参数", None, now, status="published")
            connection.execute("INSERT INTO seismic_event_audit(event_id,action,actor,after_json,created_at) VALUES(?,?,?,?,?)", (event_id, "create", actor, json.dumps(payload, ensure_ascii=False), now))
            return _row(connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()) or {}

    def get_event(self, event_id: int, include_observations: bool = True, at_version: int | None = None) -> dict[str, Any] | None:
        event = self.connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return None
        result = _row(event) or {}
        current = self._current_version(self.connection, event_id)
        head = self._head_version(self.connection, event_id)
        result["current_param_version"] = dict(current) if current else None
        result["latest_param_version"] = head["version"] if head else None
        if at_version is not None:
            snapshot = self.connection.execute(
                "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
                (event_id, at_version),
            ).fetchone()
            if snapshot is None:
                raise NotFoundError("参数版本不存在", context={"event_id": event_id, "version": at_version})
            snapshot_dict = dict(snapshot)
            for field in PARAM_FIELDS:
                result[field] = snapshot_dict[field]
            result["replay_param_version"] = snapshot_dict
            result["replay"] = True
        if include_observations:
            rows = self.connection.execute("SELECT * FROM seismic_observations WHERE event_id=? ORDER BY observed_at, id", (event_id,)).fetchall()
            result["observations"] = [dict(item) for item in rows]
        return result

    def list_versions(self, event_id: int) -> list[dict[str, Any]]:
        if self.connection.execute("SELECT 1 FROM seismic_events WHERE id=?", (event_id,)).fetchone() is None:
            raise KeyError("event_not_found")
        rows = self.connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? ORDER BY version",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_version(self, event_id: int, version: int) -> dict[str, Any]:
        if self.connection.execute("SELECT 1 FROM seismic_events WHERE id=?", (event_id,)).fetchone() is None:
            raise KeyError("event_not_found")
        row = self.connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
            (event_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError("参数版本不存在", context={"event_id": event_id, "version": version})
        return dict(row)

    def create_version(self, event_id: int, payload: dict[str, Any], actor: str = "operator") -> dict[str, Any]:
        """基于已读版本提交草稿；基线落后于最新版本时拒绝，避免静默覆盖。"""
        now = _now()
        with transaction(immediate=True) as connection:
            self._require_event(connection, event_id)
            head = self._head_version(connection, event_id)
            head_number = head["version"] if head else None
            base_version = payload.get("base_version") or head_number
            if head is not None and base_version != head_number:
                raise ConflictError(
                    "参数版本冲突：提交基于旧版本，已有人更新参数",
                    context={"event_id": event_id, "base_version": base_version, "latest_version": head_number},
                )
            values = {field: (payload[field] if payload.get(field) is not None else head[field]) for field in PARAM_FIELDS}
            created = self._insert_version(connection, event_id, values, actor, payload.get("change_reason", ""), base_version, now, status="draft")
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,after_json,created_at) VALUES(?,?,?,?,?)",
                (event_id, f"version.create:v{created['version']}", actor, json.dumps(created, ensure_ascii=False), now),
            )
            return created

    def publish_version(self, event_id: int, version: int, actor: str = "operator", reason: str = "") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            self._require_event(connection, event_id)
            row = connection.execute(
                "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
                (event_id, version),
            ).fetchone()
            if row is None:
                raise NotFoundError("参数版本不存在", context={"event_id": event_id, "version": version})
            target = dict(row)
            if target["status"] != "draft":
                raise ConflictError(
                    "只有草稿版本可以发布",
                    context={"event_id": event_id, "version": version, "status": target["status"]},
                )
            published = self._publish_locked(connection, event_id, target, now)
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, f"version.publish:v{version}", actor, json.dumps(target, ensure_ascii=False), json.dumps(published, ensure_ascii=False), now),
            )
            return published

    def revoke_version(self, event_id: int, version: int, actor: str = "operator", reason: str = "") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            self._require_event(connection, event_id)
            row = connection.execute(
                "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
                (event_id, version),
            ).fetchone()
            if row is None:
                raise NotFoundError("参数版本不存在", context={"event_id": event_id, "version": version})
            target = dict(row)
            if target["status"] == "revoked":
                raise ConflictError("版本已撤销，不能重复操作", context={"event_id": event_id, "version": version})
            was_current = target["status"] == "published"
            connection.execute(
                "UPDATE seismic_event_versions SET status='revoked', revoked_at=COALESCE(revoked_at,?) WHERE event_id=? AND version=?",
                (now, event_id, version),
            )
            target["status"] = "revoked"
            target["revoked_at"] = target["revoked_at"] or now
            after_event: dict[str, Any] | None = None
            # 撤回当前生效版本时，恢复被它替代的上一版本；没有可恢复版本则当前生效版本留空。
            if was_current:
                predecessor = connection.execute(
                    "SELECT * FROM seismic_event_versions WHERE event_id=? AND superseded_by_version=? ORDER BY version DESC LIMIT 1",
                    (event_id, version),
                ).fetchone()
                if predecessor is not None:
                    pred = dict(predecessor)
                    connection.execute(
                        "UPDATE seismic_event_versions SET status='published', superseded_by_version=NULL, revoked_at=NULL WHERE id=?",
                        (pred["id"],),
                    )
                    connection.execute(
                        "UPDATE seismic_events SET depth_km=?,magnitude=?,magnitude_type=?,version=?,updated_at=? WHERE id=?",
                        (pred["depth_km"], pred["magnitude"], pred["magnitude_type"], pred["version"], now, event_id),
                    )
                    after_event = dict(connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone())
            connection.execute(
                "INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, f"version.revoke:v{version}:{reason}", actor, json.dumps(target, ensure_ascii=False), json.dumps(after_event or {}, ensure_ascii=False), now),
            )
            return dict(connection.execute(
                "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
                (event_id, version),
            ).fetchone())

    def patch_event(self, event_id: int, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            current = self._require_event(connection, event_id)
            before = dict(current)
            status_value = payload.get("status")
            values = {key: payload[key] for key in PARAM_FIELDS if payload.get(key) is not None}
            if not status_value and not values:
                return before
            now = _now()
            if status_value:
                connection.execute("UPDATE seismic_events SET status=?, updated_at=? WHERE id=?", (status_value, now, event_id))
            if values:
                head = self._head_version(connection, event_id)
                base_version = payload.get("base_version")
                if base_version is not None and head is not None and base_version != head["version"]:
                    raise ConflictError(
                        "参数版本冲突：修改基于旧版本，已有人更新参数",
                        context={"event_id": event_id, "base_version": base_version, "latest_version": head["version"]},
                    )
                # 兼容版本表上线前建档的老事件：以事件主表当前值为基线。
                baseline = head if head is not None else current
                merged = {field: values.get(field, baseline[field]) for field in PARAM_FIELDS}
                created = self._insert_version(connection, event_id, merged, actor, payload.get("reason", ""), head["version"] if head else None, now, status="draft")
                self._publish_locked(connection, event_id, created, now)
            after = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
            connection.execute("INSERT INTO seismic_event_audit(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)", (event_id, "patch:" + payload.get("reason", ""), actor, json.dumps(before, ensure_ascii=False), json.dumps(dict(after), ensure_ascii=False), now))
            return dict(after)

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

    def _grid(self, event: sqlite3.Row | dict[str, Any], observations: list[sqlite3.Row], step: float, radius: float) -> list[GridPoint]:
        center_lat, center_lon = float(event["latitude"]), float(event["longitude"])
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
                    estimate = float(event["magnitude"]) - math.log10(max(1, float(item["distance_km"]))) + (float(item["pga"] or 0) * 0.01)
                    values.append((estimate * weight, weight))
                intensity = round(sum(value for value, _ in values) / sum(weight for _, weight in values), 3) if values else round(float(event["magnitude"]) - 1, 3)
                result.append(GridPoint(round(lat, 6), round(lon, 6), intensity))
        return result

    def _resolve_param_version(self, connection: sqlite3.Connection, event_id: int, requested_version: int | None) -> dict[str, Any]:
        if requested_version is None:
            current = self._current_version(connection, event_id)
            if current is None:
                raise ConflictError("事件尚无已发布的参数版本，不能计算", context={"event_id": event_id})
            return dict(current)
        row = connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
            (event_id, requested_version),
        ).fetchone()
        if row is None:
            raise NotFoundError("参数版本不存在", context={"event_id": event_id, "version": requested_version})
        return dict(row)

    def enqueue_computation(self, event_id: int, model_version: str, grid_step_km: float, radius_km: float, requested_by: str, param_version: int | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            event = self._require_event(connection, event_id)
            version_row = self._resolve_param_version(connection, event_id, param_version)
            observations = connection.execute("SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
            # 摘要以不可变版本快照为参数输入，事件主表随后被修订也不影响同版本回放的去重判定。
            snapshot_event = dict(event)
            for field in PARAM_FIELDS:
                snapshot_event[field] = version_row[field]
            digest = _event_digest(snapshot_event, observations, version_row)
            task_key = hashlib.sha256(
                f"{event_id}:v{version_row['version']}:{digest}:{model_version}:{grid_step_km}:{radius_km}".encode()
            ).hexdigest()
            now = _now()
            existing = connection.execute("SELECT * FROM seismic_computations WHERE task_key=?", (task_key,)).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO seismic_computations(event_id,task_key,model_version,input_digest,param_version,param_status,grid_step_km,radius_km,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, task_key, model_version, digest, version_row["version"], version_row["status"], grid_step_km, radius_km, now, now),
            )
            connection.execute("INSERT INTO seismic_event_audit(event_id,action,actor,after_json,created_at) VALUES(?,?,?,?,?)", (event_id, "compute.enqueue", requested_by, json.dumps({"task_key": task_key, "model_version": model_version, "param_version": version_row["version"], "param_status": version_row["status"]}, ensure_ascii=False), now))
            return dict(connection.execute("SELECT * FROM seismic_computations WHERE id=?", (cursor.lastrowid,)).fetchone())

    def claim_task(self, worker_id: str) -> dict[str, Any] | None:
        now = _now()
        with transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM seismic_computations WHERE status IN ('queued','retry') ORDER BY created_at,id LIMIT 1").fetchone()
            if task is None:
                return None
            connection.execute("UPDATE seismic_computations SET status='leased', attempts=attempts+1, lease_owner=?, lease_until=?, updated_at=? WHERE id=?", (worker_id, now, now, task["id"]))
            return dict(connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task["id"],)).fetchone())

    def complete_task(self, task_id: int, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
            if task is None or task["status"] != "leased" or task["lease_owner"] != worker_id:
                raise KeyError("task_not_owned")
            now = _now()
            connection.execute("UPDATE seismic_computations SET status='done',result_json=?,lease_owner='',lease_until='',updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), now, task_id))
            return dict(connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone())

    def calculate_task(self, task_id: int, worker_id: str) -> dict[str, Any]:
        task = self.connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
        if task is None or task["status"] != "leased" or task["lease_owner"] != worker_id:
            raise KeyError("task_not_owned")
        event = self.connection.execute("SELECT * FROM seismic_events WHERE id=?", (task["event_id"],)).fetchone()
        version_row = self.connection.execute(
            "SELECT * FROM seismic_event_versions WHERE event_id=? AND version=?",
            (task["event_id"], task["param_version"]),
        ).fetchone()
        observations = self.connection.execute("SELECT * FROM seismic_observations WHERE event_id=? ORDER BY id", (task["event_id"],)).fetchall()
        # 始终以入队时引用的不可变参数快照作为计算输入，新版本发布不影响在途/回放任务。
        snapshot_event = dict(event)
        if version_row is not None:
            for field in PARAM_FIELDS:
                snapshot_event[field] = version_row[field]
        points = self._grid(snapshot_event, observations, task["grid_step_km"], task["radius_km"])
        result = {
            "model_version": task["model_version"],
            "input_digest": task["input_digest"],
            "param_version": task["param_version"],
            "param_status": task["param_status"],
            "param_snapshot": {field: snapshot_event[field] for field in PARAM_FIELDS} if version_row is not None else None,
            "points": [point.__dict__ for point in points],
            "count": len(points),
        }
        return self.complete_task(task_id, worker_id, result)
