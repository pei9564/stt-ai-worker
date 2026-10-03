"""API 的回應結構，以及 domain Job → 回應結構的轉換。"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from app.domain.job import Job, JobStatus


def _iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class JobCreated(BaseModel):
    """POST 轉檔端點的回應。"""

    job_id: str
    status: str


class FeedQueued(BaseModel):
    """POST /transcribe_feed 的回應。"""

    feed_job_ids: list[str]
    count: int


class JobView(BaseModel):
    """單一 job 的對外樣貌。done / error 才會有對應的額外欄位。"""

    id: str
    type: str
    status: str
    topic: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    language: str | None = None
    duration: float | None = None
    text: str | None = None
    srt: str | None = None
    transcript_id: int | None = None
    error: str | None = None
    favorite: bool = False
    tags: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    stage: str = 'queued'
    progress: int | None = None
    error_code: str | None = None
    retry_hint: str | None = None


class DeletedResponse(BaseModel):
    """DELETE /job/{id} 的回應。"""

    deleted: str
    transcript_deleted: int | None = None


class CancelResponse(BaseModel):
    """POST /job/{id}/cancel 的回應。"""

    job_id: str
    status: str


class CancelAllResponse(BaseModel):
    """POST /jobs/cancel-all 的回應。"""

    cancelled: int
    message: str


class JobListResponse(BaseModel):
    """GET /jobs 的回應。"""

    jobs: list[JobView]
    queue_size: int


class HealthResponse(BaseModel):
    """GET /health 的回應。"""

    status: str
    model: str
    device: str
    batch_size: int
    queue_size: int


def to_job_view(job: Job, *, include_text: bool) -> JobView:
    """把 domain Job 轉成 API 回應。

    include_text=False (清單) 時，done 的 job 只附 transcript_id，不附逐字稿全文。
    """
    from app.infrastructure.audio_sources import serialize_source
    try:
        source = serialize_source(job.source) or {}
    except ValueError:
        source = {'kind': 'custom'}
    source.pop('path', None)  # never expose server filesystem paths
    view = JobView(
        provenance={'source': source, 'model_profile': job.details.get('model_profile'),
                    'model_config': job.details.get('model_config'),
                    'requested_language': job.language, 'batch_size': job.batch_size,
                    'input_truncated': False},
        favorite=bool(job.details.get("library", {}).get("favorite", False)),
        tags=job.details.get("library", {}).get("tags", []),
        id=job.id,
        type=job.type.value,
        status=job.status.value,
        topic=job.topic,
        created_at=_iso(job.created_at),
        started_at=_iso(job.started_at),
        finished_at=_iso(job.finished_at),
        stage=job.details.get('stage', job.status.value),
        progress=job.details.get('progress'),
        error_code=job.details.get('error_code'),
        retry_hint=job.details.get('retry_hint'),
    )
    if job.status == JobStatus.DONE and job.result is not None:
        view.transcript_id = job.result.transcript_id
        if include_text:
            view.language = job.result.language
            view.duration = job.result.duration
            view.text = job.result.text
            view.srt = job.result.srt
    elif job.status == JobStatus.ERROR:
        view.error = job.error
    return view
