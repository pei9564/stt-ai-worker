# STT AI Worker

語音轉文字 (Speech-to-Text) 推論服務。支援貼上 YouTube 連結或訂閱 Podcast RSS，自動下載並交由 `faster-whisper` 模型轉換成逐字稿。

本服務內建佇列機制：所有轉錄請求將排進單一佇列，由單個後台 Worker 依序處理，藉此確保 GPU VRAM 的獨佔與存取安全。本專案為 `ai-finance-infra` 生態系的一環。

## 執行

```bash
docker compose up -d --build
```

開 <http://localhost:8000> 操作，API 文件在 <http://localhost:8000/docs>。
設定改 `.env` (模型 / 裝置 / 批次大小 / 語言 / `DB_URL`)；改完 `docker compose up -d`。
逐字稿與 job 狀態都存進 Postgres (`transcripts` / `jobs` 兩張表)；服務啟動時自動套用 `app/infrastructure/schema.sql`。

## 架構

採分層架構，相依方向一律由外往內 (api → services → domain)，
infrastructure 實作 domain 定義的 port。

```
app/
  asgi.py            正式啟動進入點 (uvicorn app.asgi:app)
  main.py            create_app() 工廠 / 組裝根
  container.py       相依容器
  config.py          環境變數 → Settings
  domain/            純實體與介面，不依賴框架
    job.py             Job / JobStatus / JobType / JobResult
    transcript.py      TranscriptResult / TranscriptSegment
    audio_source.py    AudioSource 抽象介面
    ports.py           Transcriber / JobRepository / TranscriptRepository
  services/          使用情境，只依賴 domain port
    job_service.py     job 生命週期 + 佇列
    transcription.py   取得音檔 → 轉文字 → 寫進 transcripts 表
    worker.py          背景 worker thread
  infrastructure/    與外部世界互動的實作
    transcriber.py            faster-whisper 引擎 (延遲 import)
    repository.py             InMemoryJobRepository (測試用)
    pg_job_repository.py      PostgresJobRepository (正式)
    transcript_repository.py  InMemory / Postgres 兩版 transcript repo
    schema.sql                啟動時套用,idempotent
    audio_sources.py          yt-dlp / 直連下載 / RSS 解析
    gpu.py · srt.py · naming.py
  api/               FastAPI 路由 / 回應結構 / Web UI
    routes.py · schemas.py · deps.py · web.py
    templates/index.html
```

**要新增一種音檔來源** (例如雲端硬碟連結)：在 `infrastructure/audio_sources.py`
加一個 `AudioSource` 子類別，再於對應路由建立它即可，其餘流程不用動。

## 測試

單元測試以假的 `Transcriber` 取代模型，不需要 GPU：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest
```
