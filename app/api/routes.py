"""HTTP 路由。路由本身很薄：解析輸入 → 呼叫服務 → 套用回應結構。"""

from __future__ import annotations

import os
import pathlib
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.api.deps import get_container
from app.api.schemas import (
    CancelAllResponse,
    FeedQueued,
    HealthResponse,
    JobCreated,
    JobListResponse,
    JobView,
    to_job_view,
)
from app.container import Container
from app.domain.job import JobType
from app.infrastructure.audio_sources import (
    DirectUrlSource,
    LocalFileSource,
    YtDlpSource,
    parse_feed,
)
from app.infrastructure.gpu import read_gpu_stats

router = APIRouter()

_LANG_DESC = "語言代碼，如 zh / en / ja；留空自動偵測"
_BATCH_DESC = "批次大小，越大越快越耗 VRAM；1 為不批次"


# ---- 系統 ----
@router.get("/", response_class=HTMLResponse, tags=["系統"], summary="Web UI 操作介面")
def index(container: Container = Depends(get_container)) -> str:
    """瀏覽器操作介面：上傳檔案 / 貼連結 / 訂閱 RSS，以及「任務」分頁查看 48 小時內的 job。"""
    return container.web_ui.render_index()


@router.get("/health", tags=["系統"], summary="健康檢查")
def health(container: Container = Depends(get_container)) -> HealthResponse:
    """回報服務狀態、目前載入的模型 / 裝置 / 預設批次大小，以及佇列中待處理的 job 數量。"""
    s = container.settings
    return HealthResponse(
        status="ok",
        model=s.model,
        device=s.device,
        batch_size=s.batch_size,
        queue_size=container.job_service.queue_size,
    )


@router.get("/gpu", tags=["系統"], summary="GPU 即時狀態")
def gpu() -> dict:
    """讀取 nvidia-smi：GPU 使用率、VRAM 用量、功耗、溫度。容器若讀不到 GPU 則 `available` 為 false。"""
    return read_gpu_stats()


# ---- 任務查詢 ----
@router.get("/jobs", tags=["任務查詢"], summary="列出近 48 小時的 job")
def jobs(container: Container = Depends(get_container)) -> JobListResponse:
    """回傳時間窗內所有 job 的摘要 (最新在前) 與目前佇列長度。不含逐字稿全文。"""
    views = [to_job_view(job, include_text=False) for job in container.job_service.list_recent()]
    return JobListResponse(jobs=views, queue_size=container.job_service.queue_size)


@router.post("/jobs/cancel-all", tags=["任務查詢"], summary="取消所有排隊中的 job")
def cancel_all(container: Container = Depends(get_container)) -> CancelAllResponse:
    """把所有 queued 狀態的 job 標記為 error。正在執行中的 job 無法中斷，會跑完。"""
    n = container.job_service.cancel_all_queued()
    return CancelAllResponse(cancelled=n, message=f"已取消 {n} 個排隊中的 job")


@router.get("/job/{job_id}", tags=["任務查詢"], summary="查詢單一 job 狀態與結果")
def get_job(job_id: str, container: Container = Depends(get_container)) -> JobView:
    """查詢單一 job。done 時附逐字稿全文；error 時附錯誤訊息；不存在回 404。"""
    job = container.job_service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return to_job_view(job, include_text=True)


# ---- 轉檔 ----
@router.post("/transcribe", status_code=202, tags=["轉檔"], summary="上傳本地音檔轉文字")
async def transcribe(
    file: UploadFile = File(..., description="音檔 (mp3 / m4a / wav / 影片檔皆可)"),
    language: str = Form("", description=_LANG_DESC),
    batch_size: int = Form(0, description=_BATCH_DESC),
    topic: str = Form("", description="輸出檔名 output/{日期}/{topic}.txt；留空用上傳檔名"),
    container: Container = Depends(get_container),
) -> JobCreated:
    """上傳音檔加入轉檔佇列。立即回傳 `job_id`，請輪詢 `GET /job/{job_id}` 取得結果。"""
    suffix = pathlib.Path(file.filename or "").suffix or ".audio"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(await file.read())

    source = LocalFileSource(tmp_path, file.filename or "audio")
    job = container.job_service.create(
        job_type=JobType.FILE,
        source=source,
        topic=topic,
        language=language,
        batch_size=batch_size,
    )
    return JobCreated(job_id=job.id, status=job.status.value)


@router.post("/transcribe_url", status_code=202, tags=["轉檔"], summary="從 YouTube / 影音連結轉文字")
def transcribe_url(
    url: str = Form(..., description="YouTube 或其他影音 / podcast 單集網址，由 yt-dlp 抽取音訊"),
    language: str = Form("", description=_LANG_DESC),
    batch_size: int = Form(0, description=_BATCH_DESC),
    topic: str = Form("", description="輸出檔名 output/{日期}/{topic}.txt；留空用影片標題"),
    container: Container = Depends(get_container),
) -> JobCreated:
    """貼一個影音網址加入轉檔佇列。立即回傳 `job_id`，請輪詢 `GET /job/{job_id}` 取得結果。"""
    job = container.job_service.create(
        job_type=JobType.URL,
        source=YtDlpSource(url),
        topic=topic,
        language=language,
        batch_size=batch_size,
    )
    return JobCreated(job_id=job.id, status=job.status.value)


@router.post("/transcribe_feed", status_code=202, tags=["轉檔"], summary="從 Podcast RSS 批次轉文字")
def transcribe_feed(
    feed_url: str = Form(..., description="Podcast RSS feed 網址"),
    limit: int = Form(0, description="抓最新幾集；0 表示整季全抓"),
    language: str = Form("", description=_LANG_DESC),
    batch_size: int = Form(0, description=_BATCH_DESC),
    container: Container = Depends(get_container),
) -> FeedQueued:
    """解析 RSS，將每一集各自加入佇列 (每集一個獨立 job)。每集的 topic 自動用單集標題。"""
    try:
        episodes = parse_feed(feed_url, limit)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_ids = [
        container.job_service.create(
            job_type=JobType.FEED_EPISODE,
            source=DirectUrlSource(ep.audio_url, ep.title),
            topic=ep.title,
            language=language,
            batch_size=batch_size,
        ).id
        for ep in episodes
    ]
    return FeedQueued(feed_job_ids=job_ids, count=len(job_ids))
