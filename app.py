import datetime
import json
import os
import pathlib
import queue
import re
import subprocess
import tempfile
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

import feedparser
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from faster_whisper import BatchedInferencePipeline, WhisperModel

MODEL = os.getenv("MODEL", "medium")
DEVICE = os.getenv("DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "int8")
LANGUAGE = os.getenv("LANGUAGE") or None
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))

OUTPUT_DIR = pathlib.Path("/data/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR = OUTPUT_DIR / ".jobs"  # job 紀錄持久化到這裡 (每個 job 一個 JSON)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ---- job 佇列：單一 worker thread 序列化 GPU 存取 ----
JOB_QUEUE: "queue.Queue[str | None]" = queue.Queue()
JOBS: "OrderedDict[str, dict]" = OrderedDict()
JOBS_LOCK = threading.Lock()
MAX_JOBS = 200          # JOBS 超過就淘汰最舊的 done/error 紀錄
JOBS_WINDOW_HOURS = 48  # /jobs 只回傳這個時間窗內的紀錄


def log(msg: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[stt] {stamp} {msg}", flush=True)


def gpu_stats() -> dict:
    """讀 nvidia-smi 即時數據，供 UI 輪詢。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[-200:]}
        util, mem_used, mem_total, power, temp = (x.strip() for x in out.stdout.strip().split(","))
        return {
            "available": True,
            "util": float(util),
            "mem_used": float(mem_used),
            "mem_total": float(mem_total),
            "power": None if power.upper().startswith("N/A") else float(power),
            "temp": float(temp),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


print(f"[stt] loading model={MODEL} device={DEVICE} compute_type={COMPUTE_TYPE}", flush=True)
model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
batched_model = BatchedInferencePipeline(model=model)
print("[stt] model ready", flush=True)


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _safe_stem(name: str) -> str:
    stem = pathlib.Path(name).stem
    stem = re.sub(r"[^\w一-鿿 -]", "_", stem).strip()
    return stem[:80] or "audio"


def run_transcription(
    audio_path: pathlib.Path,
    source_name: str,
    language: str | None,
    batch_size: int | None = None,
    topic: str | None = None,
) -> dict:
    """轉檔核心：吃一個本地音檔路徑，輸出到 OUTPUT_DIR/{日期}/{topic}.{txt,srt}。

    batch_size: None=用 BATCH_SIZE 環境預設; 1=不批次; >1=用批次推論加速。
    topic: 指定就用它當檔名，否則用 source_name 自動產生。
    """
    lang = (language or "").strip() or LANGUAGE
    bs = BATCH_SIZE if batch_size is None else batch_size
    if bs and bs > 1:
        segments, info = batched_model.transcribe(
            str(audio_path), language=lang, batch_size=bs, vad_filter=True
        )
    else:
        segments, info = model.transcribe(str(audio_path), language=lang, vad_filter=True)

    text_lines, srt_blocks = [], []
    for i, seg in enumerate(segments, 1):
        line = seg.text.strip()
        text_lines.append(line)
        srt_blocks.append(f"{i}\n{_srt_time(seg.start)} --> {_srt_time(seg.end)}\n{line}\n")

    text = "\n".join(text_lines)

    day = datetime.date.today().isoformat()
    day_dir = OUTPUT_DIR / day
    day_dir.mkdir(parents=True, exist_ok=True)
    base = _safe_stem(topic or source_name)
    stem, n = base, 2
    while (day_dir / f"{stem}.txt").exists():
        stem = f"{base}_{n}"
        n += 1
    (day_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    (day_dir / f"{stem}.srt").write_text("\n".join(srt_blocks), encoding="utf-8")

    return {
        "language": info.language,
        "duration": info.duration,
        "text": text,
        "txt_file": f"{day}/{stem}.txt",
        "srt_file": f"{day}/{stem}.srt",
    }


def download_with_ytdlp(url: str, dest_dir: pathlib.Path) -> pathlib.Path:
    """用 yt-dlp 從 YouTube / 各影音站 / podcast 單集頁抽出音訊。"""
    out_tmpl = str(dest_dir / "%(title).80s.%(ext)s")
    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
        "--no-progress", "-o", out_tmpl, "--print", "after_move:filepath", url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp 失敗: {result.stderr.strip()[-400:]}")
    return pathlib.Path(result.stdout.strip().splitlines()[-1])


def download_file(url: str, dest_dir: pathlib.Path, name_hint: str) -> pathlib.Path:
    """直接下載音檔連結 (RSS enclosure 多半是直連 mp3)。"""
    dest = dest_dir / f"{_safe_stem(name_hint)}.mp3"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    return dest


def feed_episodes(feed_url: str, limit: int) -> list[tuple[str, str]]:
    """解析 RSS，回傳 [(標題, 音檔URL), ...]，limit=0 表示整季全抓。"""
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS 解析失敗: {parsed.bozo_exception}")
    episodes: list[tuple[str, str]] = []
    for entry in parsed.entries:
        audio_url = None
        for enc in entry.get("enclosures", []):
            href = enc.get("href", "")
            if "audio" in enc.get("type", "") or href.lower().split("?")[0].endswith(
                (".mp3", ".m4a", ".aac", ".ogg", ".wav")
            ):
                audio_url = href
                break
        if audio_url:
            episodes.append((entry.get("title", "episode"), audio_url))
    return episodes[:limit] if limit else episodes


# ---- 佇列 / job 管理 ----
def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _persist(job_id: str) -> None:
    """把 job 紀錄寫到磁碟。呼叫者必須已持有 JOBS_LOCK。"""
    rec = JOBS.get(job_id)
    if rec is None:
        return
    data = {k: v for k, v in rec.items() if k != "payload"}
    try:
        (JOBS_DIR / f"{job_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log(f"[persist] {job_id} 寫入失敗: {e}")


def _load_persisted() -> None:
    """啟動時載入磁碟上的 job 紀錄；佇列中/執行中的標記為 error (服務重啟未完成)。"""
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for stale in files[:-MAX_JOBS]:  # 超過上限的最舊紀錄直接刪
        stale.unlink(missing_ok=True)
    loaded = 0
    with JOBS_LOCK:
        for f in files[-MAX_JOBS:]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") in ("queued", "running"):
                data["status"] = "error"
                data["error"] = "服務重啟，job 未完成"
                data["finished_at"] = data.get("finished_at") or _now()
            data["payload"] = None  # 重啟後無法復原原始輸入
            JOBS[data["id"]] = data
            _persist(data["id"])
            loaded += 1
    if loaded:
        log(f"[startup] 載入 {loaded} 筆歷史 job 紀錄")


def _set_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)
            _persist(job_id)


def new_job(job_type: str, topic: str | None, language: str | None,
            batch_size: int, payload) -> str:
    """建立一筆 job 紀錄、丟進佇列，回傳 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "type": job_type, "status": "queued",
            "topic": topic or None, "language": language or None,
            "batch_size": batch_size, "payload": payload,
            "created_at": _now(), "started_at": None, "finished_at": None,
            "result": None, "error": None,
        }
        _persist(job_id)
        while len(JOBS) > MAX_JOBS:
            for jid in list(JOBS):
                if JOBS[jid]["status"] in ("done", "error"):
                    del JOBS[jid]
                    (JOBS_DIR / f"{jid}.json").unlink(missing_ok=True)
                    break
            else:
                break  # 沒有可淘汰的 (全部還在佇列/執行中)
    JOB_QUEUE.put(job_id)
    return job_id


def _run_job(job: dict) -> dict:
    """依 job 類型取得音檔並轉檔，回傳 run_transcription 的結果 dict。"""
    jtype, language, batch_size, topic = (
        job["type"], job["language"], job["batch_size"], job["topic"]
    )
    payload = job["payload"]
    if jtype == "file":
        tmp_path_str, original_name = payload
        audio_path = pathlib.Path(tmp_path_str)
        try:
            return run_transcription(audio_path, original_name, language, batch_size, topic)
        finally:
            audio_path.unlink(missing_ok=True)
    elif jtype == "url":
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = download_with_ytdlp(payload, pathlib.Path(tmp))
            return run_transcription(audio_path, audio_path.name, language, batch_size, topic)
    elif jtype == "feed_episode":
        title, audio_url = payload
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = download_file(audio_url, pathlib.Path(tmp), title)
            return run_transcription(audio_path, title, language, batch_size, topic)
    raise RuntimeError(f"未知 job 類型: {jtype}")


def worker() -> None:
    """單一 worker thread：一次處理一個 job，序列化 GPU 存取。"""
    log("[worker] thread started")
    while True:
        job_id = JOB_QUEUE.get()
        try:
            if job_id is None:  # shutdown sentinel
                break
            with JOBS_LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                continue
            _set_job(job_id, status="running", started_at=_now())
            log(f"[job {job_id}] 開始 ({job['type']})")
            try:
                result = _run_job(job)
                _set_job(job_id, status="done", result=result, finished_at=_now())
                log(f"[job {job_id}] 完成 → {result['txt_file']}")
            except Exception as e:
                _set_job(job_id, status="error", error=str(e), finished_at=_now())
                log(f"[job {job_id}] 失敗: {e}")
        finally:
            JOB_QUEUE.task_done()
    log("[worker] thread stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_persisted()
    t = threading.Thread(target=worker, daemon=True, name="stt-worker")
    t.start()
    yield
    JOB_QUEUE.put(None)  # 通知 worker 結束


app = FastAPI(title="Podcast STT", lifespan=lifespan)


def _job_view(rec: dict, include_text: bool) -> dict:
    """把內部 job 紀錄轉成 API 回傳格式 (不含 payload)。"""
    view = {
        "id": rec["id"], "type": rec["type"], "status": rec["status"],
        "topic": rec["topic"], "created_at": rec["created_at"],
        "started_at": rec["started_at"], "finished_at": rec["finished_at"],
    }
    if rec["status"] == "done" and rec["result"]:
        if include_text:
            view.update(rec["result"])
        else:
            view["txt_file"] = rec["result"]["txt_file"]
    elif rec["status"] == "error":
        view["error"] = rec["error"]
    return view


PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Podcast 轉文字</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.4rem; }
  .tabs { display: flex; gap: 4px; margin-bottom: -1px; }
  .tab { padding: 8px 16px; border: 1px solid #ccc; border-bottom: none; border-radius: 8px 8px 0 0;
         cursor: pointer; background: #f3f3f3; }
  .tab.active { background: #fff; font-weight: 600; }
  .box { border: 1px solid #ccc; border-radius: 0 8px 8px 8px; padding: 20px; }
  .panel { display: none; }
  .panel.active { display: block; }
  label { display: block; margin: 12px 0 4px; font-weight: 600; }
  input, select { padding: 6px; font-size: 1rem; }
  input[type=text] { width: 100%; box-sizing: border-box; }
  button { margin-top: 16px; padding: 8px 20px; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  #status, #feedStatus { margin-top: 16px; color: #555; white-space: pre-wrap; }
  textarea { width: 100%; height: 280px; margin-top: 12px; font-family: monospace; }
  .meta { font-size: 0.85rem; color: #777; }
  .hint { font-size: 0.8rem; color: #999; font-weight: normal; }
  .gpubar { font-size: 0.85rem; padding: 8px 12px; border-radius: 6px; margin-bottom: 14px;
            background: #f0f4f8; border: 1px solid #d8e0e8; font-family: monospace; }
  .gpubar.warm { background: #fff3e0; border-color: #ffcc80; }
  .gpubar.hot { background: #ffebee; border-color: #ef9a9a; }
  .jobrow { padding: 8px 6px; border-bottom: 1px solid #eee; font-size: 0.9rem; }
  .jobrow.clickable { cursor: pointer; }
  .jobrow.clickable:hover { background: #fafafa; }
  .badge { display: inline-block; min-width: 52px; text-align: center; padding: 1px 8px;
           border-radius: 10px; font-size: 0.75rem; color: #fff; margin-right: 6px; }
  .badge.queued { background: #90a4ae; }
  .badge.running { background: #1976d2; }
  .badge.done { background: #2e7d32; }
  .badge.error { background: #c62828; }
</style>
</head>
<body>
<h1>Podcast 轉文字 (STT)</h1>
<p class="meta">模型: __MODEL__ | 裝置: __DEVICE__ | 預設批次: __BATCH__</p>
<div id="gpu" class="gpubar">GPU: 載入中…</div>

<div class="tabs">
  <div class="tab active" data-tab="file">本地檔案</div>
  <div class="tab" data-tab="url">YouTube / 連結</div>
  <div class="tab" data-tab="feed">RSS 整季</div>
  <div class="tab" data-tab="jobs">任務</div>
</div>
<div class="box">

  <div class="panel active" id="panel-file">
    <label>音檔 (mp3 / m4a / wav / ...)</label>
    <input type="file" id="file" accept="audio/*,video/*">
    <label>語言</label>
    <select id="lang-file">__LANGOPTS__</select>
    <label>批次大小 <span class="hint">越大越快、GPU 越熱</span></label>
    <select id="batch-file">__BATCHOPTS__</select>
    <label>Topic <span class="hint">選填，存成 output/日期/topic.txt；留空用檔名</span></label>
    <input type="text" id="topic-file" placeholder="(選填)">
    <button onclick="doTranscribe('file')">加入佇列</button>
  </div>

  <div class="panel" id="panel-url">
    <label>YouTube 或影音 / podcast 單集網址</label>
    <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=...">
    <label>語言</label>
    <select id="lang-url">__LANGOPTS__</select>
    <label>批次大小 <span class="hint">越大越快、GPU 越熱</span></label>
    <select id="batch-url">__BATCHOPTS__</select>
    <label>Topic <span class="hint">選填，存成 output/日期/topic.txt；留空用影片標題</span></label>
    <input type="text" id="topic-url" placeholder="(選填)">
    <button onclick="doTranscribe('url')">加入佇列</button>
  </div>

  <div class="panel" id="panel-feed">
    <label>Podcast RSS Feed 網址</label>
    <input type="text" id="feed" placeholder="https://.../feed.xml">
    <label>抓幾集 (0 = 整季全抓，從最新往回)</label>
    <input type="text" id="feedLimit" value="3" style="width:80px">
    <label>語言</label>
    <select id="lang-feed">__LANGOPTS__</select>
    <label>批次大小 <span class="hint">整季長時間跑，建議 1~4 較不燒機</span></label>
    <select id="batch-feed">__BATCHOPTS__</select>
    <button onclick="startFeed()">加入佇列 (每集一個 job)</button>
    <div id="feedStatus"></div>
  </div>

  <div class="panel" id="panel-jobs">
    <p class="hint">過去 48 小時內的所有 job，每 5 秒自動更新。點「done」的項目可載入逐字稿。</p>
    <div id="jobsList">載入中…</div>
  </div>

  <div id="status"></div>
  <textarea id="result" placeholder="完成的逐字稿會顯示在這；所有結果都會存到 output/{日期}/ 資料夾"></textarea>
</div>

<script>
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('panel-' + t.dataset.tab).classList.add('active');
  if (t.dataset.tab === 'jobs') startJobsPanel(); else stopJobsPanel();
});

const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');
const pollTimers = {};

async function doTranscribe(mode) {
  const fd = new FormData();
  fd.append('language', document.getElementById('lang-' + mode).value);
  fd.append('batch_size', document.getElementById('batch-' + mode).value);
  fd.append('topic', document.getElementById('topic-' + mode).value.trim());
  let url;
  if (mode === 'file') {
    const f = document.getElementById('file').files[0];
    if (!f) { statusEl.textContent = '請先選擇音檔'; return; }
    fd.append('file', f);
    url = '/transcribe';
  } else {
    const u = document.getElementById('url').value.trim();
    if (!u) { statusEl.textContent = '請輸入網址'; return; }
    fd.append('url', u);
    url = '/transcribe_url';
  }
  resultEl.value = '';
  statusEl.textContent = '送出中…';
  try {
    const r = await fetch(url, { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '送出失敗');
    pollJob(data.job_id);
  } catch (e) {
    statusEl.textContent = '錯誤: ' + e.message;
  }
}

function pollJob(jobId) {
  if (pollTimers[jobId]) clearInterval(pollTimers[jobId]);
  const tick = async () => {
    try {
      const d = await (await fetch('/job/' + jobId)).json();
      if (d.status === 'queued') {
        statusEl.textContent = `job ${jobId} 排隊中…`;
      } else if (d.status === 'running') {
        statusEl.textContent = `job ${jobId} 轉檔中…`;
      } else if (d.status === 'done') {
        clearInterval(pollTimers[jobId]);
        statusEl.textContent = `job ${jobId} 完成 | 語言 ${d.language} | 長度 ${d.duration.toFixed(1)}s | 已存 ${d.txt_file}`;
        resultEl.value = d.text;
      } else if (d.status === 'error') {
        clearInterval(pollTimers[jobId]);
        statusEl.textContent = `job ${jobId} 失敗: ${d.error}`;
      }
    } catch (e) {
      statusEl.textContent = 'job 查詢失敗: ' + e.message;
    }
  };
  pollTimers[jobId] = setInterval(tick, 3000);
  tick();
}

async function startFeed() {
  const feed = document.getElementById('feed').value.trim();
  const fs = document.getElementById('feedStatus');
  if (!feed) { fs.textContent = '請輸入 RSS 網址'; return; }
  const fd = new FormData();
  fd.append('feed_url', feed);
  fd.append('limit', document.getElementById('feedLimit').value || '0');
  fd.append('language', document.getElementById('lang-feed').value);
  fd.append('batch_size', document.getElementById('batch-feed').value);
  try {
    const r = await fetch('/transcribe_feed', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '送出失敗');
    fs.textContent = `已排入 ${data.count} 集，到「任務」分頁追蹤進度`;
  } catch (e) {
    fs.textContent = '錯誤: ' + e.message;
  }
}

let jobsTimer = null;
function startJobsPanel() {
  if (jobsTimer) clearInterval(jobsTimer);
  jobsTimer = setInterval(renderJobs, 5000);
  renderJobs();
}
function stopJobsPanel() {
  if (jobsTimer) { clearInterval(jobsTimer); jobsTimer = null; }
}

async function renderJobs() {
  const box = document.getElementById('jobsList');
  try {
    const d = await (await fetch('/jobs')).json();
    if (!d.jobs.length) { box.textContent = '(48 小時內沒有 job)'; return; }
    box.innerHTML = '';
    const header = document.createElement('div');
    header.className = 'hint';
    header.textContent = `佇列中 ${d.queue_size} 個待處理 · 共 ${d.jobs.length} 筆`;
    box.appendChild(header);
    d.jobs.forEach(j => {
      const row = document.createElement('div');
      row.className = 'jobrow' + (j.status === 'done' ? ' clickable' : '');
      const when = (j.finished_at || j.created_at || '').replace('T', ' ');
      row.innerHTML =
        `<span class="badge ${j.status}">${j.status}</span>` +
        `<b>${(j.topic || j.id)}</b> ` +
        `<span class="hint">${j.type} · ${when}</span>`;
      if (j.status === 'done') {
        row.onclick = () => viewJobResult(j.id);
      } else if (j.status === 'error') {
        const err = document.createElement('div');
        err.className = 'hint';
        err.style.color = '#c62828';
        err.textContent = j.error || '';
        row.appendChild(err);
      }
      box.appendChild(row);
    });
  } catch (e) {
    box.textContent = 'job 清單載入失敗: ' + e.message;
  }
}

async function viewJobResult(jobId) {
  statusEl.textContent = '載入逐字稿…';
  try {
    const d = await (await fetch('/job/' + jobId)).json();
    if (d.status !== 'done') { statusEl.textContent = `job ${jobId} 狀態: ${d.status}`; return; }
    statusEl.textContent = `job ${jobId} | 語言 ${d.language} | 長度 ${d.duration.toFixed(1)}s | 已存 ${d.txt_file}`;
    resultEl.value = d.text;
  } catch (e) {
    statusEl.textContent = '載入失敗: ' + e.message;
  }
}

async function pollGpu() {
  const el = document.getElementById('gpu');
  try {
    const d = await (await fetch('/gpu')).json();
    if (!d.available) { el.textContent = 'GPU: 無法讀取 ' + (d.error || ''); return; }
    const pwr = d.power != null ? d.power.toFixed(0) + 'W' : 'N/A';
    el.textContent = `GPU  使用率 ${d.util}%   VRAM ${d.mem_used.toFixed(0)}/${d.mem_total.toFixed(0)} MiB   功耗 ${pwr}   溫度 ${d.temp.toFixed(0)}°C`;
    el.className = 'gpubar' + (d.temp >= 85 ? ' hot' : d.temp >= 75 ? ' warm' : '');
  } catch (e) {
    el.textContent = 'GPU: 連線失敗';
  }
}
setInterval(pollGpu, 2000);
pollGpu();
</script>
</body>
</html>
"""

_LANG_OPTS = (
    '<option value="">自動偵測</option>'
    '<option value="zh" selected>中文 zh</option>'
    '<option value="en">英文 en</option>'
    '<option value="ja">日文 ja</option>'
)

_BATCH_CHOICES = [
    (1, "1 — 不批次（最省電、最安靜）"),
    (4, "4 — 平衡"),
    (8, "8 — 最快、最熱"),
]


def _batch_opts() -> str:
    html = ""
    matched = False
    for val, label in _BATCH_CHOICES:
        sel = " selected" if val == BATCH_SIZE else ""
        if sel:
            matched = True
        html += f'<option value="{val}"{sel}>{label}</option>'
    if not matched:
        html = f'<option value="{BATCH_SIZE}" selected>{BATCH_SIZE} (.env 設定)</option>' + html
    return html


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        PAGE.replace("__MODEL__", MODEL)
        .replace("__DEVICE__", DEVICE)
        .replace("__BATCH__", str(BATCH_SIZE))
        .replace("__LANGOPTS__", _LANG_OPTS)
        .replace("__BATCHOPTS__", _batch_opts())
    )


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "device": DEVICE, "batch_size": BATCH_SIZE,
            "queue_size": JOB_QUEUE.qsize()}


@app.get("/gpu")
def gpu():
    return gpu_stats()


@app.get("/jobs")
def jobs():
    cutoff = (
        datetime.datetime.now() - datetime.timedelta(hours=JOBS_WINDOW_HOURS)
    ).isoformat(timespec="seconds")
    with JOBS_LOCK:
        items = [
            _job_view(rec, include_text=False)
            for rec in JOBS.values()
            if rec["created_at"] >= cutoff
        ]
    items.reverse()  # 最新的在前
    return {"jobs": items, "queue_size": JOB_QUEUE.qsize()}


@app.get("/job/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        rec = JOBS.get(job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_view(rec, include_text=True)


@app.post("/transcribe", status_code=202)
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
    topic: str = Form(""),
):
    suffix = pathlib.Path(file.filename).suffix or ".audio"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(await file.read())
    job_id = new_job("file", topic, language, batch_size, (tmp_path, file.filename))
    return {"job_id": job_id, "status": "queued"}


@app.post("/transcribe_url", status_code=202)
def transcribe_url(
    url: str = Form(...),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
    topic: str = Form(""),
):
    job_id = new_job("url", topic, language, batch_size, url)
    return {"job_id": job_id, "status": "queued"}


@app.post("/transcribe_feed", status_code=202)
def transcribe_feed(
    feed_url: str = Form(...),
    limit: int = Form(0),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
):
    try:
        episodes = feed_episodes(feed_url, limit)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 每集一個獨立 job，單一 URL 中途插入只需等一集而非整季
    job_ids = [
        new_job("feed_episode", None, language, batch_size, (title, audio_url))
        for title, audio_url in episodes
    ]
    return {"feed_job_ids": job_ids, "count": len(job_ids)}
