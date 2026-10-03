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

## 任務資源限制

STT 預設上傳／下載 512 MiB、音訊最長 7200 秒、queued＋running 共 20 筆、RSS 單次 20 集、batch_size 最大 16。
0 集數表示抓到設定的上限，不再代表無限；RSS 容量不足時整批拒絕，回 429。
超大上傳回 413，低磁碟空間回 507；失敗上傳會清理暫存檔。
`MAX_UPLOAD_BYTES`、`MAX_DOWNLOAD_BYTES`、`MAX_AUDIO_SECONDS`、`MAX_PENDING_JOBS`、
`MAX_FEED_EPISODES`、`MAX_BATCH_SIZE`、`MIN_FREE_DISK_BYTES` 可調整。
下載總時間由 `DOWNLOAD_TIMEOUT_SECONDS`（600）限制，RSS 有 5 MiB／60 秒上限。
正式 STT 在 GPU 推論前用 ffprobe 驗證音訊格式／長度（測試注入的 fake model 路徑不執行 ffprobe）。

`MAX_JOB_SECONDS`（7200）逾時後將工作標為失敗、健康檢查回 503，由 K3s liveness 重啟服務。
GPU thread 不能安全硬中止；尚未回收 GPU 前不會啟動下一個任務，逾時工作不自動恢復，需手動重試。
Compose 沒有 Kubernetes liveness 控制器，逾時後請自行 restart STT。
LLM HTTP timeout 用 `LLM_TIMEOUT_SECONDS`（120），空白／格式錯誤回覆視為失敗。
scraper STT 輪詢由 `STT_TIMEOUT_SECONDS` 控制；infra 預設 7200 秒，
超時只停止等候，不會取消遠端工作，可到 STT UI 查看它。

## 任務階段與錯誤提示

STT 工作提供 `stage`、`progress`、`error_code`、`retry_hint`，PG 的 details JSONB 保存階段與進度。
下載／驗證／轉錄／儲存各有名稱，轉錄百分比依已處理音訊時間估算，更新最多約每 5 秒或 5%。
下載時間未知時不顯示假百分比；完成為 100%。失敗保留最後階段，UI 顯示可執行的重試建議。
scraper 保存遠端 STT job ID，重啟後查詢會繼續等候既有工作；不會因查詢而自動重試失敗工作。
使用者再次按送 STT 才重試原失敗工作；不存在的工作才重新提交。
翻譯／摘要沿用批次逐筆進度，API status 另附階段名稱與重試提示。

## 監控與通知

兩個服務提供 Prometheus 相容的 `/metrics`，包含 HTTP 次數／耗時、背景操作成功／失敗與耗時。
STT 另含 queued jobs、worker_up、uploads 可用磁碟；scraper 含 pipeline／STT 提交數。
labels 使用路由模板，不含 job ID／來源網址／逐字稿，避免無限 cardinality。
每個 HTTP 回應有 `X-Request-ID`，可傳入合法 ID；scraper 會轉送它到 STT／LLM，
STT 保存原提交 request ID，worker logs 用 job_id 連結處理時間。
STT `/diagnostics`、scraper `/api/diagnostics` 提供狀態與磁碟資訊。

可選 `ALERT_WEBHOOK_URL` 接受 JSON `{service, code, job_id}`，預設空白不發通知。
工作失敗／逾時、HTTP 5xx、DB 不可用與低磁碟空間會告警，同類通知 5 分鐘合併，
有限背景佇列與 5 秒 HTTP timeout 避免影響工作；投遞失敗不自動重試。
不傳送全文、URL、credentials 或 webhook 位址。服務每 60 秒檢查 DB／磁碟。
K3s Service 已加 scrape annotations；要保存歷史圖表仍需自行接 Prometheus 等收集器。
進程完全停止時無法自行通知，需外部監控／K3s 告警補足。

### 去重與保留

相同來源、模型設定、語言、批次大小與 topic 的 queued/running/done 工作會直接回傳原 job ID；URL 忽略 fragment 和 utm/fbclid/gclid，檔案以 SHA-256 判斷。失敗工作可明確 retry，重新提交也可建立新工作。此機制適用單一 STT replica。

`POST /maintenance/prune?days=7` 預覽過期失敗工作與無引用上傳暫存檔；加 `&dry_run=false` 才執行。保留天數至少 1 天，不刪正在執行、排隊或完成的逐字稿。成功上傳音檔在完成後清除；失敗音檔留供重試，超過保留期可清理。清理目前由使用者手動呼叫，建議備份後再執行。

### 來源紀錄與匯出

完成工作會顯示 JSON / Markdown / SRT 下載連結。API：`GET /job/{id}/export?format=json|markdown|srt`；未完成回 409。JSON 和 Markdown 包含來源、模型設定、語言、批次大小與工作時間，不暴露上傳音檔的伺服器路徑。歷史工作沒有紀錄的模型設定保留 null，不推測補填。

### Voice Notes 轉錄工作台

開啟 STT 服務首頁（使用 ai-finance-infra Docker Compose 時為 `http://localhost:8001`；單獨啟動預設為 `http://localhost:8000`）：

- **貼上連結**：輸入 YouTube、Podcast 單集或直接音訊網址，選擇語言後開始轉錄。
- **上傳檔案**：點選或拖入一個音訊／影片檔，畫面會顯示檔名、大小及服務設定的限制。上傳完成前請保持頁面開啟。
- **Podcast RSS**：輸入 RSS 網址與集數，由新到舊建立各集任務；集數上限依服務設定顯示。
- **任務**：搜尋近期任務，依處理中、已完成或未完成篩選；開啟任務可查看進度與逐字稿，失敗時可重試。刪除與取消全部排隊任務會先確認。
- **逐字稿**：直接閱讀、複製全文，或下載 TXT、SRT 字幕、Markdown、JSON。透過未啟用 HTTPS 的遠端地址使用時，若瀏覽器限制剪貼簿，會自動選取全文並提示手動複製。

任務建立後可離開頁面，稍後從「任務」重新開啟；清單依 `JOBS_WINDOW_HOURS` 設定顯示近期任務。進階設定包含處理批次大小，模型與 GPU 狀態收在頁面底部。手機版上下排列，來源分頁支援方向鍵、Home、End 操作。

#### 長音檔閱讀與進度

- 有 SRT 時間軸的結果預設以段落呈現；可搜尋段落、複製單段，或切換純文字並逐一跳至符合的文字。沒有可用時間軸的舊結果會直接顯示純文字。
- 上傳顯示傳送百分比與大小；檔案送完後顯示「等待服務建立任務」，此時不再提供取消上傳。中途取消會中止瀏覽器的傳送，但若資料已送達，仍應到「任務」確認是否已建立；已排隊任務可另行取消。
- 重新整理後會恢復最近查看的任務、來源分頁、任務搜尋／篩選與閱讀模式。只把這些設定存於同一瀏覽器、同一網站的 localStorage，不儲存音檔或逐字稿內容；檔案上傳本身無法跨重新整理續傳。
- 正在查看的任務完成時，分頁標題會顯示完成狀態。可從「服務狀態與模型資訊」主動開啟桌面通知；需瀏覽器支援及 localhost／HTTPS，並保持頁面開啟。重新開啟已完成的任務不會重複通知。
