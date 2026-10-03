# STT AI Worker

語音轉文字 (Speech-to-Text) 推論服務。支援上傳本地音檔、貼上 YouTube 連結或訂閱 Podcast RSS，自動下載並交由 `faster-whisper` 模型轉換成逐字稿。

本服務內建佇列機制：所有轉錄請求將排進單一佇列，由單個後台 Worker 依序處理，藉此確保 GPU VRAM 的獨佔與存取安全。本專案為 `ai-finance-infra` 生態系的一環。

## 執行

本服務已正式遷移至 K3s 叢集作為主要運行環境（部署細節請參考 `ai-finance-infra` repo）。若需在本地單獨使用 Docker Compose 開發測試：

```bash
docker compose up -d --build
```

開 <http://localhost:8000> 操作，API 文件在 <http://localhost:8000/docs>。
設定改 `.env` (模型 / 裝置 / 批次大小 / 語言 / `DB_URL`)；改完重啟即可。
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

## 任務持久化、恢復與重試

PG `jobs.source` 保存音訊來源描述。啟動時把未完成的 queued / running 任務重新排隊，從頭執行；舊紀錄若缺少來源則標記 error，需要重新提交。失敗任務可在 UI 按「重試」或呼叫 `POST /job/{job_id}/retry`，同一任務不會在 queued / running / done 時重複排隊。

上傳音檔保存於 `UPLOAD_DIR`（預設 `/data/uploads`），正式部署必須掛載持久 volume / PVC。音檔成功或刪除任務後清理，失敗及取消時保留供重試。本地直接啟動可設定 `UPLOAD_DIR=./uploads`。URL 來源重試時會重新下載。

佇列使用程序內 `queue.Queue`；Redis 尚未使用。只支援一個 replica 和一個 uvicorn process，不能同時啟動多個 worker 共用這些 jobs。

`/healthz` 檢查 worker 是否存活；`/readyz` 額外檢查 PostgreSQL。K3s startup probe 給模型載入時間，避免啟動時被 liveness probe 重啟。

PostgreSQL 整合測試必須使用隔離 DB（測試會清空 jobs / transcripts）：

```bash
TEST_PG_DSN=postgresql://test:test@localhost:5432/test .venv/bin/python -m pytest -q
```

整合測試使用假轉錄模型，驗證真正 PG 的來源還原、服務重啟與失敗重試，無需 GPU。
