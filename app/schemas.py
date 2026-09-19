"""请求与响应模型（OpenAPI 由此生成）。"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Confidence = Literal["baseline", "high", "medium", "low"]


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class ReadingIn(BaseModel):
    device_sn: str = Field(..., min_length=1, max_length=64, examples=["CASE-007"])
    ts: dt.datetime = Field(..., description="ISO8601 时间戳；无时区按 UTC 处理", examples=["2026-09-01T08:00:00+08:00"])
    temperature: float = Field(..., ge=-50.0, le=100.0, examples=[20.4])
    humidity: float = Field(..., ge=0.0, le=100.0, examples=[55.2])

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: dt.datetime) -> dt.datetime:
        return _to_utc(v)


class BatchIngest(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1, max_length=10000)
    penalty: float | None = Field(None, ge=0.0, description="本次重放使用的惩罚项；缺省沿用展柜已保存值或默认值")
    replay: bool = Field(True, description="入库后是否立即重放受影响展柜的时间线")


class AnchorIn(BaseModel):
    device_sn: str = Field(..., min_length=1, max_length=64)
    ts: dt.datetime
    label: str | None = Field(None, max_length=128)
    ref_temperature: float | None = Field(None, ge=-50.0, le=100.0)
    ref_humidity: float | None = Field(None, ge=0.0, le=100.0)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: dt.datetime) -> dt.datetime:
        return _to_utc(v)


class SegmentOut(BaseModel):
    index: int
    start_ts: dt.datetime
    end_ts: dt.datetime
    sample_count: int
    mean_temperature: float
    mean_humidity: float
    temperature_std: float
    humidity_std: float
    delta_temperature: float
    delta_humidity: float
    magnitude: float = Field(..., description="缩放加权后的欧氏变化幅度")
    confidence: Confidence
    start_anchored: bool
    end_anchored: bool
    cost: float


class AnchorOut(BaseModel):
    device_sn: str
    ts: dt.datetime
    label: str | None
    ref_temperature: float | None
    ref_humidity: float | None
    aligned: bool
    aligned_ts: dt.datetime | None = Field(None, description="实际对齐到的读数时间戳")


class SegmentationOut(BaseModel):
    device_sn: str
    version: int
    penalty: float
    sample_count: int
    anchor_count: int
    total_cost: float
    replayed: bool
    replayed_at: dt.datetime | None
    unmatched_anchors: list[dt.datetime]
    segments: list[SegmentOut]


class IngestSummary(BaseModel):
    device_sn: str
    inserted: int
    merged: int
    affected: bool = Field(..., description="该展柜是否被重放（存在新读数或合并）")
    before_version: int
    after_version: int
    replay: "SegmentationOut | None" = None


class IngestResponse(BaseModel):
    accepted: int
    inserted: int
    merged: int
    replayed_devices: list[str]
    summaries: list[IngestSummary]


class CaseOut(BaseModel):
    device_sn: str
    version: int
    penalty: float
    reading_count: int
    anchor_count: int
    replayed_at: dt.datetime | None


class ReplayPreviewResponse(BaseModel):
    device_sn: str
    current_version: int
    would_bump_version: bool
    changed: bool = Field(..., description="预览分段与当前已存储分段是否不同")
    penalty: float
    added_readings: int
    added_anchors: int
    affected_from_ts: dt.datetime | None
    affected_to_ts: dt.datetime | None
    preview: SegmentationOut
    current: "SegmentationOut | None" = None


class HealthOut(BaseModel):
    status: str
    db: str


IngestSummary.model_rebuild()
ReplayPreviewResponse.model_rebuild()
