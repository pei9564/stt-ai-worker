# Podcast STT

語音轉文字服務。上傳音檔 / 貼 YouTube 連結 / 訂閱 Podcast RSS，由 faster-whisper
轉成逐字稿。所有請求排進單一佇列，由一個 worker 依序處理 (序列化 GPU 存取)。

## 執行

```bash
docker compose up -d --build
```

開 <http://localhost:8000> 操作，API 文件在 <http://localhost:8000/docs>。
設定改 `.env` (模型 / 裝置 / 批次大小 / 語言)；改完 `docker compose up -d`。
逐字稿輸出在 `output/{日期}/{topic}.{txt,srt}`。

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
    ports.py           Transcriber / JobRepository 介面
  services/          使用情境，只依賴 domain port
    job_service.py     job 生命週期 + 佇列
    transcription.py   取得音檔 → 轉文字 → 寫檔
    worker.py          背景 worker thread
  infrastructure/    與外部世界互動的實作
    transcriber.py     faster-whisper 引擎 (延遲 import)
    repository.py      JSON 檔 / 記憶體 job 儲存
    audio_sources.py   yt-dlp / 直連下載 / RSS 解析
    output_writer.py   逐字稿落地
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
