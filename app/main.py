"""文物展柜环境变点分析 API。"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import services
from .database import CaseState, SessionLocal, init_db, to_utc
from .schemas import (
    AnchorIn,
    AnchorOut,
    BatchIngest,
    CaseOut,
    HealthOut,
    IngestResponse,
    IngestSummary,
    ReadingIn,
    ReplayPreviewResponse,
    SegmentationOut,
)
from .segmentation import Sample, segment_samples


class PreviewBody(BaseModel):
    """重放预览的试算输入（全部仅在内存中处理，不写库）。"""

    readings: list[ReadingIn] = Field(default_factory=list, description="待补入的（含迟到的）读数")
    anchors: list[AnchorIn] = Field(default_factory=list, description="待补入的锚点")
    penalty: float | None = Field(None, ge=0.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="文物展柜环境变点分析 API",
    description=(
        "接收带设备序号与时间戳的温湿度读数，按惩罚项对每个展柜执行"
        "确定性变点分段；合并同序号重复上报，支持迟到读数补入后的"
        "时间线重放、锚点校准与无写库重放预览。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_ts(value: dt.datetime | None) -> dt.datetime | None:
    return to_utc(value) if value is not None else None


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health():
    return HealthOut(status="ok", db="sqlite")


# ---------- 读数上报 ----------


@app.post(
    "/api/v1/readings/batch",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    tags=["readings"],
    summary="批量上报温湿度读数（自动合并重复上报并重放）",
)
def ingest_readings(payload: BatchIngest, db: Session = Depends(get_db)):
    stats = services.merge_readings(db, payload.readings)

    summaries: list[IngestSummary] = []
    replayed_devices: list[str] = []
    total_inserted = total_merged = 0

    for device_sn in sorted(stats):
        inserted = stats[device_sn]["inserted"]
        merged = stats[device_sn]["merged"]
        total_inserted += inserted
        total_merged += merged

        state = db.get(CaseState, device_sn)
        before_version = state.version if state else 0

        replay_out = None
        if payload.replay and (inserted or merged):
            replay_out = services.replay_device(
                db, device_sn, penalty=payload.penalty, changed=True
            )
            replayed_devices.append(device_sn)
        after_version = replay_out.version if replay_out else before_version

        summaries.append(
            IngestSummary(
                device_sn=device_sn,
                inserted=inserted,
                merged=merged,
                affected=bool(inserted or merged),
                before_version=before_version,
                after_version=after_version,
                replay=replay_out,
            )
        )

    db.commit()
    return IngestResponse(
        accepted=len(payload.readings),
        inserted=total_inserted,
        merged=total_merged,
        replayed_devices=replayed_devices,
        summaries=summaries,
    )


# ---------- 展柜与分段查询 ----------


@app.get("/api/v1/cases", response_model=list[CaseOut], tags=["cases"], summary="展柜列表")
def list_cases(db: Session = Depends(get_db)):
    return [
        CaseOut(
            device_sn=state.device_sn,
            version=state.version,
            penalty=state.penalty,
            reading_count=state.reading_count,
            anchor_count=services._count_anchors(db, state.device_sn),
            replayed_at=to_utc(state.replayed_at) if state.replayed_at else None,
        )
        for state in services.list_cases(db)
    ]


@app.get(
    "/api/v1/cases/{device_sn}/segments",
    response_model=SegmentationOut,
    tags=["cases"],
    summary="查询某展柜的变点分段（可按时间范围过滤）",
)
def get_segments(
    device_sn: str,
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    db: Session = Depends(get_db),
):
    stored = services.get_stored_segmentation(db, device_sn)
    if stored is None:
        # 允许对尚未重放的展柜即时计算（只读，不落库）
        samples = services._load_samples(db, device_sn)
        if not samples:
            raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
        anchors = services._load_anchor_ts(db, device_sn)
        result = segment_samples(device_sn, samples, anchors)
        stored = services.serialize_result(result, version=0, replayed=False, replayed_at=None)

    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc or end_utc:
        lo = start_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        hi = end_utc or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        if lo > hi:
            raise HTTPException(status_code=422, detail="start 不能晚于 end")
        kept = [s for s in stored.segments if s.end_ts >= lo and s.start_ts <= hi]
        stored = stored.model_copy(update={"segments": kept})
    return stored


# ---------- 锚点校准 ----------


@app.post(
    "/api/v1/anchors",
    response_model=AnchorOut,
    tags=["anchors"],
    summary="登记/更新锚点（强制变点边界）并重放",
)
def add_anchor(payload: AnchorIn, db: Session = Depends(get_db)):
    anchor = services.upsert_anchor(db, payload)
    services.replay_device(db, payload.device_sn, changed=True)
    db.commit()
    info = services.list_anchors(db, payload.device_sn)
    return next(item for item in info if item["ts"] == to_utc(anchor.ts))


@app.get(
    "/api/v1/cases/{device_sn}/anchors",
    response_model=list[AnchorOut],
    tags=["anchors"],
    summary="查询锚点及其对齐情况",
)
def get_anchors(device_sn: str, db: Session = Depends(get_db)):
    return services.list_anchors(db, device_sn)


@app.delete(
    "/api/v1/cases/{device_sn}/anchors/{anchor_ts}",
    response_model=SegmentationOut,
    tags=["anchors"],
    summary="删除锚点并重放该展柜",
)
def remove_anchor(
    device_sn: str,
    anchor_ts: dt.datetime,
    db: Session = Depends(get_db),
):
    anchor_ts = _parse_ts(anchor_ts)
    if not services.delete_anchor(db, device_sn, anchor_ts):
        raise HTTPException(status_code=404, detail="锚点不存在")
    out = services.replay_device(db, device_sn, changed=True)
    db.commit()
    return out


# ---------- 重放预览（严禁写库） ----------


@app.post(
    "/api/v1/replay/preview",
    response_model=ReplayPreviewResponse,
    tags=["replay"],
    summary="重放预览：基于待补入的迟到读数/锚点试算，绝不写库",
)
def replay_preview(
    body: PreviewBody,
    device_sn: str = Query(..., description="展柜设备序号"),
    db: Session = Depends(get_db),
):
    pending = [r for r in body.readings if r.device_sn == device_sn]
    pending_anchors = [a for a in body.anchors if a.device_sn == device_sn]

    # 当前已持久化状态
    current = services.get_stored_segmentation(db, device_sn)
    current_version = current.version if current else 0

    # 在内存中合并“已存储读数 + 待补入读数”，全程不加入数据库会话
    samples = services._load_samples(db, device_sn)
    merged_keys: dict[dt.datetime, Sample] = {s.ts: s for s in samples}
    added = 0
    for r in pending:
        existing = merged_keys.get(r.ts)
        if existing is None:
            merged_keys[r.ts] = Sample(ts=r.ts, temperature=r.temperature, humidity=r.humidity)
        else:
            # Sample 不可变：在内存副本上按 report_count 逐条累计均值，
            # 合并规则与 merge_readings 一致；库内读数、快照、版本号均不受影响
            total = existing.report_count + 1
            merged_keys[r.ts] = Sample(
                ts=existing.ts,
                temperature=(
                    existing.temperature * existing.report_count + r.temperature
                ) / total,
                humidity=(
                    existing.humidity * existing.report_count + r.humidity
                ) / total,
                report_count=total,
            )
        added += 1

    anchor_set = set(services._load_anchor_ts(db, device_sn))
    for a in pending_anchors:
        anchor_set.add(a.ts)

    result = segment_samples(
        device_sn,
        list(merged_keys.values()),
        list(anchor_set),
        penalty=body.penalty,
    )
    preview = services.serialize_result(
        result,
        version=current_version + 1,
        replayed=False,
        replayed_at=None,
    )

    affected_from, affected_to, changed = services.affected_window(current, preview)

    return ReplayPreviewResponse(
        device_sn=device_sn,
        current_version=current_version,
        would_bump_version=changed,
        changed=changed,
        penalty=result.penalty,
        added_readings=added,
        added_anchors=len(pending_anchors),
        affected_from_ts=affected_from,
        affected_to_ts=affected_to,
        preview=preview,
        current=current,
    )
