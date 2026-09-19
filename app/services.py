"""业务逻辑：重复上报合并、确定性重放、锚点校准与无写预览。"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import segmentation as seg
from .config import settings
from .database import Anchor, CaseState, Reading, SegmentSnapshot, to_utc, utcnow
from .schemas import AnchorIn, ReadingIn, SegmentOut, SegmentationOut


# ---------- 读取 / 序列化 ----------


def _load_samples(db: Session, device_sn: str) -> list[seg.Sample]:
    rows = db.scalars(
        select(Reading)
        .where(Reading.device_sn == device_sn)
        .order_by(Reading.ts.asc())
    ).all()
    return [
        seg.Sample(
            ts=to_utc(r.ts),
            temperature=r.temperature,
            humidity=r.humidity,
            report_count=r.report_count,
        )
        for r in rows
    ]


def _load_anchor_ts(db: Session, device_sn: str) -> list[dt.datetime]:
    rows = db.scalars(
        select(Anchor.ts).where(Anchor.device_sn == device_sn)
    ).all()
    return [to_utc(t) for t in rows]


def _snapshot_rows(db: Session, device_sn: str) -> list[SegmentSnapshot]:
    return list(
        db.scalars(
            select(SegmentSnapshot)
            .where(SegmentSnapshot.device_sn == device_sn)
            .order_by(SegmentSnapshot.segment_index.asc())
        ).all()
    )


def serialize_result(
    result: seg.SegmentationResult,
    version: int,
    replayed: bool,
    replayed_at: dt.datetime | None,
) -> SegmentationOut:
    return SegmentationOut(
        device_sn=result.device_sn,
        version=version,
        penalty=result.penalty,
        sample_count=result.sample_count,
        anchor_count=result.anchor_count,
        total_cost=result.total_cost,
        replayed=replayed,
        replayed_at=replayed_at,
        unmatched_anchors=list(result.unmatched_anchors),
        segments=[
            SegmentOut(
                index=s.index,
                start_ts=s.start_ts,
                end_ts=s.end_ts,
                sample_count=s.sample_count,
                mean_temperature=s.mean_temperature,
                mean_humidity=s.mean_humidity,
                temperature_std=s.temperature_std,
                humidity_std=s.humidity_std,
                delta_temperature=s.delta_temperature,
                delta_humidity=s.delta_humidity,
                magnitude=s.magnitude,
                confidence=s.confidence,
                start_anchored=s.start_anchored,
                end_anchored=s.end_anchored,
                cost=s.cost,
            )
            for s in result.segments
        ],
    )


def get_stored_segmentation(db: Session, device_sn: str) -> SegmentationOut | None:
    state = db.get(CaseState, device_sn)
    rows = _snapshot_rows(db, device_sn)
    if state is None or not rows:
        return None
    return SegmentationOut(
        device_sn=device_sn,
        version=state.version,
        penalty=state.penalty,
        sample_count=state.reading_count,
        anchor_count=_count_anchors(db, device_sn),
        total_cost=sum(r.cost for r in rows),
        replayed=False,
        replayed_at=to_utc(state.replayed_at) if state.replayed_at else None,
        unmatched_anchors=[],
        segments=[_snapshot_to_out(r, i) for i, r in enumerate(rows)],
    )


def _snapshot_to_out(r: SegmentSnapshot, i: int) -> SegmentOut:
    return SegmentOut(
        index=i,
        start_ts=to_utc(r.start_ts),
        end_ts=to_utc(r.end_ts),
        sample_count=r.sample_count,
        mean_temperature=r.mean_temperature,
        mean_humidity=r.mean_humidity,
        temperature_std=0.0,
        humidity_std=0.0,
        delta_temperature=r.delta_temperature,
        delta_humidity=r.delta_humidity,
        magnitude=r.magnitude,
        confidence=r.confidence,
        start_anchored=bool(r.start_anchored),
        end_anchored=bool(r.end_anchored),
        cost=r.cost,
    )


def _count_anchors(db: Session, device_sn: str) -> int:
    return int(
        db.scalar(select(func.count(Anchor.id)).where(Anchor.device_sn == device_sn)) or 0
    )


# ---------- 重复上报合并 ----------


def merge_readings(
    db: Session, readings: list[ReadingIn]
) -> dict[str, dict[str, int]]:
    """按 (device_sn, ts) 幂等合并。

    - 新时间戳：直接插入；
    - 已存在：以 report_count 为权把温湿度并入累计均值，
      report_count 加一（确定性的合并规则，重复重放结果稳定）。

    返回 {device_sn: {"inserted": n, "merged": n}}。
    """
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"inserted": 0, "merged": 0})

    existing: dict[tuple[str, dt.datetime], Reading] = {}
    device_sns = {r.device_sn for r in readings}
    for sn in device_sns:
        for row in db.scalars(select(Reading).where(Reading.device_sn == sn)).all():
            existing[(sn, to_utc(row.ts))] = row

    for item in readings:
        key = (item.device_sn, item.ts)
        row = existing.get(key)
        if row is None:
            row = Reading(
                device_sn=item.device_sn,
                ts=item.ts,
                temperature=item.temperature,
                humidity=item.humidity,
                report_count=1,
            )
            db.add(row)
            existing[key] = row
            stats[item.device_sn]["inserted"] += 1
        else:
            total = row.report_count + 1
            row.temperature = (row.temperature * row.report_count + item.temperature) / total
            row.humidity = (row.humidity * row.report_count + item.humidity) / total
            row.report_count = total
            stats[item.device_sn]["merged"] += 1

    db.flush()
    return stats


# ---------- 重放 ----------


def _compute(
    db: Session, device_sn: str, penalty: float | None = None
) -> seg.SegmentationResult:
    samples = _load_samples(db, device_sn)
    anchors = _load_anchor_ts(db, device_sn)
    beta = penalty if penalty is not None else settings.PENALTY
    state = db.get(CaseState, device_sn)
    if penalty is None and state is not None:
        beta = state.penalty
    return seg.segment_samples(device_sn, samples, anchors, penalty=beta)


def _persist(db: Session, result: seg.SegmentationResult, state: CaseState) -> None:
    db.execute(delete(SegmentSnapshot).where(SegmentSnapshot.device_sn == result.device_sn))
    for s in result.segments:
        db.add(
            SegmentSnapshot(
                device_sn=result.device_sn,
                version=state.version,
                segment_index=s.index,
                start_ts=s.start_ts,
                end_ts=s.end_ts,
                sample_count=s.sample_count,
                mean_temperature=s.mean_temperature,
                mean_humidity=s.mean_humidity,
                delta_temperature=s.delta_temperature,
                delta_humidity=s.delta_humidity,
                magnitude=s.magnitude,
                confidence=s.confidence,
                start_anchored=s.start_anchored,
                end_anchored=s.end_anchored,
                cost=s.cost,
            )
        )


def replay_device(
    db: Session,
    device_sn: str,
    penalty: float | None = None,
    changed: bool = True,
) -> SegmentationOut:
    """对单个展柜整条时间线执行确定性重放并落库。

    ``changed=False`` 时仅重新计算并刷新快照（用于首次生成等场景），
    不增加版本号；正常的数据/锚点变更调用应保持 ``changed=True``。
    """
    result = _compute(db, device_sn, penalty=penalty)

    state = db.get(CaseState, device_sn)
    if state is None:
        state = CaseState(device_sn=device_sn, version=0, penalty=result.penalty)
        db.add(state)
    if penalty is not None:
        state.penalty = result.penalty
    if changed:
        # 读数/锚点发生变化；版本号单调递增
        state.version += 1
    state.reading_count = result.sample_count
    state.replayed_at = utcnow()

    _persist(db, result, state)
    db.flush()
    return serialize_result(result, state.version, True, to_utc(state.replayed_at))


def recompute_if_needed(
    db: Session, device_sn: str, penalty: float | None = None
) -> SegmentationOut | None:
    """读数有新增/合并时重放；空展柜不产生版本。"""
    count = db.scalar(
        select(func.count(Reading.id)).where(Reading.device_sn == device_sn)
    ) or 0
    if count == 0:
        return None
    return replay_device(db, device_sn, penalty=penalty)


# ---------- 锚点 ----------


def upsert_anchor(db: Session, payload: AnchorIn) -> Anchor:
    existing = db.scalar(
        select(Anchor).where(
            Anchor.device_sn == payload.device_sn, Anchor.ts == payload.ts
        )
    )
    if existing is None:
        existing = Anchor(device_sn=payload.device_sn, ts=payload.ts)
        db.add(existing)
    existing.label = payload.label
    existing.ref_temperature = payload.ref_temperature
    existing.ref_humidity = payload.ref_humidity
    _align_anchor(db, existing)
    db.flush()
    return existing


def _align_anchor(db: Session, anchor: Anchor) -> None:
    nearest = db.scalars(
        select(Reading)
        .where(Reading.device_sn == anchor.device_sn)
        .order_by(func.abs(func.julianday(Reading.ts) - func.julianday(anchor.ts)))
    ).first()
    if (
        nearest is not None
        and abs((to_utc(nearest.ts) - to_utc(anchor.ts)).total_seconds())
        <= settings.ANCHOR_TOLERANCE
    ):
        anchor.reading_id = nearest.id
    else:
        anchor.reading_id = None


def list_anchors(db: Session, device_sn: str) -> list[dict]:
    rows = db.scalars(
        select(Anchor).where(Anchor.device_sn == device_sn).order_by(Anchor.ts.asc())
    ).all()
    out = []
    for a in rows:
        aligned_ts = to_utc(a.reading.ts) if a.reading is not None else None
        out.append(
            {
                "device_sn": a.device_sn,
                "ts": to_utc(a.ts),
                "label": a.label,
                "ref_temperature": a.ref_temperature,
                "ref_humidity": a.ref_humidity,
                "aligned": a.reading_id is not None,
                "aligned_ts": aligned_ts,
            }
        )
    return out


def delete_anchor(db: Session, device_sn: str, ts: dt.datetime) -> bool:
    anchor = db.scalar(
        select(Anchor).where(Anchor.device_sn == device_sn, Anchor.ts == ts)
    )
    if anchor is None:
        return False
    db.delete(anchor)
    db.flush()
    return True


def list_cases(db: Session) -> list[CaseState]:
    return list(db.scalars(select(CaseState).order_by(CaseState.device_sn.asc())).all())


def affected_window(
    current: SegmentationOut | None, preview: SegmentationOut
) -> tuple[dt.datetime | None, dt.datetime | None, bool]:
    """比较已存储分段与预览分段，给出受影响时间范围。

    迟到读数补入后，从第一个发生变化的段起点起，到时间线末尾都视为
    受影响（后续均值会被重新估计）。
    """
    if current is None or not current.segments:
        if preview.segments:
            return (
                preview.segments[0].start_ts,
                preview.segments[-1].end_ts,
                True,
            )
        return None, None, False

    changed = _segmentation_differs(current, preview)
    if not changed:
        return None, None, False

    first_diff = preview.segments[0].start_ts
    for cur, new in zip(current.segments, preview.segments):
        if (
            cur.start_ts != new.start_ts
            or cur.end_ts != new.end_ts
            or cur.sample_count != new.sample_count
            or abs(cur.mean_temperature - new.mean_temperature) > 1e-9
            or abs(cur.mean_humidity - new.mean_humidity) > 1e-9
        ):
            first_diff = new.start_ts
            break
    else:  # 长度相同但存在其他差异（如惩罚项）
        first_diff = preview.segments[-1].start_ts

    return first_diff, preview.segments[-1].end_ts, True


def _segmentation_differs(a: SegmentationOut, b: SegmentationOut) -> bool:
    if a.penalty != b.penalty:
        return True
    sa, sb = a.segments, b.segments
    if len(sa) != len(sb):
        return True
    for x, y in zip(sa, sb):
        if (
            x.start_ts != y.start_ts
            or x.end_ts != y.end_ts
            or x.sample_count != y.sample_count
            or abs(x.mean_temperature - y.mean_temperature) > 1e-9
            or abs(x.mean_humidity - y.mean_humidity) > 1e-9
        ):
            return True
    return False
