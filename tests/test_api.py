"""集成测试：使用内存 SQLite，互不影响。"""

from __future__ import annotations

import os

os.environ["CASE_DB_URL"] = "sqlite:///:memory:"
os.environ["CASE_PENALTY"] = "4.0"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, CaseState, Reading, SessionLocal, engine, init_db
from app.main import app, get_db


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _reading(sn, ts, t, h):
    return {"device_sn": sn, "ts": ts, "temperature": t, "humidity": h}


# ---------- 基础 ----------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openapi_available(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"]


# ---------- 变点分段 ----------


def test_changepoint_detected_with_confidence(client):
    payload = []
    # 前 6 点 ~20.0/55，后 6 点 ~24.0/70（大幅、稳定的跳变）
    for i in range(6):
        h = 8 + i
        payload.append(_reading("CASE-1", f"2026-09-01T08:{h:02d}:00+08:00", 20.0, 55.0))
    for i in range(6):
        h = 20 + i
        payload.append(_reading("CASE-1", f"2026-09-01T09:{h:02d}:00+08:00", 24.0, 70.0))

    r = client.post("/api/v1/readings/batch", json={"readings": payload})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 12
    replay = body["summaries"][0]["replay"]
    confs = [s["confidence"] for s in replay["segments"]]
    assert confs[0] == "baseline"
    assert "high" in confs
    # 跳变段方向与幅度
    jump = next(s for s in replay["segments"] if s["confidence"] == "high")
    assert jump["delta_temperature"] == pytest.approx(4.0, abs=1e-6)
    assert jump["delta_humidity"] == pytest.approx(15.0, abs=1e-6)
    assert jump["magnitude"] > 0
    assert jump["start_ts"].startswith("2026-09-01T01:20:00")  # UTC


def test_single_segment_when_stable(client):
    payload = [
        _reading("CASE-2", f"2026-09-01T08:{i:02d}:00+08:00", 20.0 + 0.01 * (i % 2), 55.0)
        for i in range(8)
    ]
    r = client.post("/api/v1/readings/batch", json={"readings": payload})
    segs = r.json()["summaries"][0]["replay"]["segments"]
    assert len(segs) == 1
    assert segs[0]["confidence"] == "baseline"


def test_deterministic(client):
    payload = []
    for i in range(5):
        payload.append(_reading("CASE-3", f"2026-09-01T08:{i:02d}:00+08:00", 20.0, 55.0))
    for i in range(5):
        payload.append(_reading("CASE-3", f"2026-09-01T09:{i:02d}:00+08:00", 22.5, 60.0))
    # 乱序上报
    shuffled = payload[::2] + payload[1::2]
    r1 = client.post("/api/v1/readings/batch", json={"readings": shuffled})
    s1 = r1.json()["summaries"][0]["replay"]

    # 第二次同样数据重放（全部命中合并）分段结构应完全一致
    r2 = client.post("/api/v1/readings/batch", json={"readings": payload})
    s2 = r2.json()["summaries"][0]["replay"]
    key = lambda s: [(x["start_ts"], x["end_ts"], x["sample_count"]) for x in s["segments"]]
    assert key(s1) == key(s2)


def test_penalty_monotonicity(client):
    payload = [
        _reading("CASE-4", f"2026-09-01T08:{i:02d}:00+08:00",
                 20.0 + (1.5 if i >= 4 else 0.0), 55.0)
        for i in range(8)
    ]
    low = client.post("/api/v1/readings/batch",
                      json={"readings": payload, "penalty": 0.5}).json()
    n_low = len(low["summaries"][0]["replay"]["segments"])

    high = client.post("/api/v1/readings/batch",
                       json={"readings": payload, "penalty": 100.0}).json()
    n_high = len(high["summaries"][0]["replay"]["segments"])
    assert n_low >= n_high
    assert n_high == 1


# ---------- 重复合并 ----------


def test_duplicate_reports_merged(client, db):
    base = [_reading("CASE-5", "2026-09-01T08:00:00+08:00", 20.0, 55.0)]
    client.post("/api/v1/readings/batch", json={"readings": base})
    again = [
        _reading("CASE-5", "2026-09-01T08:00:00+08:00", 22.0, 60.0),
        _reading("CASE-5", "2026-09-01T08:10:00+08:00", 21.0, 56.0),
    ]
    r = client.post("/api/v1/readings/batch", json={"readings": again})
    body = r.json()
    assert body["inserted"] == 1
    assert body["merged"] == 1

    db.expire_all()
    row = db.query(Reading).filter(Reading.device_sn == "CASE-5").order_by(Reading.ts).first()
    assert row.report_count == 2
    assert row.temperature == pytest.approx(21.0)
    assert row.humidity == pytest.approx(57.5)


# ---------- 迟到读数重放 ----------


def test_late_reading_replays_timeline(client):
    # 先上两段
    payload = [
        _reading("CASE-6", "2026-09-01T08:00:00+08:00", 20.0, 55.0),
        _reading("CASE-6", "2026-09-01T08:10:00+08:00", 20.0, 55.0),
        _reading("CASE-6", "2026-09-01T08:20:00+08:00", 20.0, 55.0),
        _reading("CASE-6", "2026-09-01T09:00:00+08:00", 20.0, 55.0),
        _reading("CASE-6", "2026-09-01T09:10:00+08:00", 20.0, 55.0),
        _reading("CASE-6", "2026-09-01T09:20:00+08:00", 20.0, 55.0),
    ]
    r1 = client.post("/api/v1/readings/batch", json={"readings": payload})
    v1 = r1.json()["summaries"][0]["after_version"]
    n1 = len(r1.json()["summaries"][0]["replay"]["segments"])

    # 迟到一个中间时刻的高读数
    late = [_reading("CASE-6", "2026-09-01T08:40:00+08:00", 26.0, 75.0)]
    r2 = client.post("/api/v1/readings/batch", json={"readings": late})
    summary = r2.json()["summaries"][0]
    assert summary["after_version"] == v1 + 1
    n2 = len(summary["replay"]["segments"])
    assert n2 >= n1


def test_multi_device_isolation(client):
    payload = [
        _reading("A", "2026-09-01T08:00:00+08:00", 20.0, 55.0),
        _reading("B", "2026-09-01T08:00:00+08:00", 20.0, 55.0),
    ]
    r = client.post("/api/v1/readings/batch", json={"readings": payload})
    sns = {s["device_sn"] for s in r.json()["summaries"]}
    assert sns == {"A", "B"}
    assert set(r.json()["replayed_devices"]) == {"A", "B"}


# ---------- 锚点 ----------


def test_anchor_forces_boundary(client):
    payload = [
        _reading("CASE-7", f"2026-09-01T08:{i * 5:02d}:00+08:00", 20.0, 55.0)
        for i in range(10)
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})
    # 完全平稳的数据：无锚点时只有一段
    segs = client.get("/api/v1/cases/CASE-7/segments").json()["segments"]
    assert len(segs) == 1

    r = client.post("/api/v1/anchors", json={
        "device_sn": "CASE-7",
        "ts": "2026-09-01T08:25:00+08:00",
        "label": "开柜检查",
    })
    assert r.status_code == 200, r.text
    anchor = r.json()
    assert anchor["aligned"] is True
    assert anchor["aligned_ts"] == "2026-09-01T00:25:00Z"  # 统一 UTC 输出

    segs = client.get("/api/v1/cases/CASE-7/segments").json()["segments"]
    assert len(segs) == 2
    assert segs[1]["start_anchored"] is True
    assert segs[1]["confidence"] == "high"


def test_anchor_delete_replays(client):
    payload = [
        _reading("CASE-8", f"2026-09-01T08:{i * 5:02d}:00+08:00", 20.0, 55.0)
        for i in range(10)
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})
    client.post("/api/v1/anchors", json={
        "device_sn": "CASE-8", "ts": "2026-09-01T08:30:00+08:00"})
    assert len(client.get("/api/v1/cases/CASE-8/segments").json()["segments"]) == 2

    r = client.delete("/api/v1/cases/CASE-8/anchors/2026-09-01T08:30:00+08:00")
    assert r.status_code == 200
    assert len(r.json()["segments"]) == 1


# ---------- 查询 ----------


def test_segment_time_filter(client):
    payload = [
        _reading("CASE-9", "2026-09-01T08:00:00+08:00", 20.0, 55.0),
        _reading("CASE-9", "2026-09-01T08:10:00+08:00", 20.0, 55.0),
        _reading("CASE-9", "2026-09-01T08:20:00+08:00", 20.0, 55.0),
        _reading("CASE-9", "2026-09-01T09:00:00+08:00", 24.0, 70.0),
        _reading("CASE-9", "2026-09-01T09:10:00+08:00", 24.0, 70.0),
        _reading("CASE-9", "2026-09-01T09:20:00+08:00", 24.0, 70.0),
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})
    r = client.get("/api/v1/cases/CASE-9/segments", params={
        "start": "2026-09-01T09:00:00+08:00",
        "end": "2026-09-01T10:00:00+08:00",
    })
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["mean_temperature"] == pytest.approx(24.0)


def test_unknown_case_404(client):
    r = client.get("/api/v1/cases/NOPE/segments")
    assert r.status_code == 404


# ---------- 重放预览：不得写库 ----------


def test_preview_does_not_write(client, db):
    payload = [
        _reading("CASE-P", "2026-09-01T08:00:00+08:00", 20.0, 55.0),
        _reading("CASE-P", "2026-09-01T08:10:00+08:00", 20.0, 55.0),
        _reading("CASE-P", "2026-09-01T08:20:00+08:00", 20.0, 55.0),
        _reading("CASE-P", "2026-09-01T08:30:00+08:00", 20.0, 55.0),
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})

    version_before = db.get(CaseState, "CASE-P").version
    count_before = db.query(Reading).filter(Reading.device_sn == "CASE-P").count()

    r = client.post("/api/v1/replay/preview", params={"device_sn": "CASE-P"}, json={
        "readings": [
            _reading("CASE-P", "2026-09-01T09:00:00+08:00", 26.0, 75.0),
            _reading("CASE-P", "2026-09-01T09:10:00+08:00", 26.0, 75.0),
            _reading("CASE-P", "2026-09-01T09:20:00+08:00", 26.0, 75.0),
        ]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["would_bump_version"] is True
    assert body["current_version"] == version_before
    assert body["preview"]["version"] == version_before + 1
    assert body["preview"]["replayed"] is False
    assert body["affected_from_ts"] is not None
    # 库内读数数量与版本号不变
    db.expire_all()
    assert db.query(Reading).filter(Reading.device_sn == "CASE-P").count() == count_before
    assert db.get(CaseState, "CASE-P").version == version_before

    # 预览中出现的变点在实际分段里尚不存在
    stored = client.get("/api/v1/cases/CASE-P/segments").json()
    assert len(stored["segments"]) == len(body["current"]["segments"])


def test_preview_with_anchor_does_not_write(client, db):
    payload = [
        _reading("CASE-Q", f"2026-09-01T08:{i * 5:02d}:00+08:00", 20.0, 55.0)
        for i in range(8)
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})
    r = client.post("/api/v1/replay/preview", params={"device_sn": "CASE-Q"}, json={
        "anchors": [{"device_sn": "CASE-Q", "ts": "2026-09-01T08:20:00+08:00"}]
    })
    body = r.json()
    assert body["added_anchors"] == 1
    assert len(body["preview"]["segments"]) == 2
    # 库内仍然只有一段、没有锚点
    stored = client.get("/api/v1/cases/CASE-Q/segments").json()
    assert len(stored["segments"]) == 1
    assert client.get("/api/v1/cases/CASE-Q/anchors").json() == []


def test_preview_identical_when_no_change(client):
    payload = [
        _reading("CASE-R", f"2026-09-01T08:{i * 5:02d}:00+08:00",
                 20.0 + (0.02 if i % 2 else -0.02), 55.0)
        for i in range(8)
    ]
    client.post("/api/v1/readings/batch", json={"readings": payload})
    r = client.post("/api/v1/replay/preview", params={"device_sn": "CASE-R"}, json={})
    body = r.json()
    assert body["changed"] is False
    assert body["would_bump_version"] is False
    assert body["affected_from_ts"] is None
