import datetime
import os
import pathlib
import re
import subprocess
import tempfile

import feedparser
import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from faster_whisper import BatchedInferencePipeline, WhisperModel

MODEL = os.getenv("MODEL", "medium")
DEVICE = os.getenv("DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "int8")
LANGUAGE = os.getenv("LANGUAGE") or None
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))

OUTPUT_DIR = pathlib.Path("/data/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 背景任務 (RSS 整季) 的進度紀錄，只留最近 100 筆，供網頁輪詢
JOB_LOG: list[str] = []


def log(msg: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{stamp} {msg}"
    print(f"[stt] {line}", flush=True)
    JOB_LOG.append(line)
    del JOB_LOG[:-100]


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

app = FastAPI(title="Podcast STT")


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
) -> dict:
    """轉檔核心：吃一個本地音檔路徑，輸出 .txt / .srt 到 OUTPUT_DIR。

    batch_size: None=用 BATCH_SIZE 環境預設; 1=不批次; >1=用批次推論加速。
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
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{_safe_stem(source_name)}_{ts}"
    txt_file, srt_file = f"{stem}.txt", f"{stem}.srt"
    (OUTPUT_DIR / txt_file).write_text(text, encoding="utf-8")
    (OUTPUT_DIR / srt_file).write_text("\n".join(srt_blocks), encoding="utf-8")

    return {
        "language": info.language,
        "duration": info.duration,
        "text": text,
        "txt_file": txt_file,
        "srt_file": srt_file,
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


def process_feed(feed_url: str, limit: int, language: str | None, batch_size: int) -> None:
    """背景任務：逐集下載 + 轉檔，進度寫進 JOB_LOG。"""
    try:
        episodes = feed_episodes(feed_url, limit)
    except Exception as e:
        log(f"[RSS] 解析失敗: {e}")
        return
    log(f"[RSS] 共 {len(episodes)} 集排入佇列 (batch_size={batch_size})")
    for idx, (title, audio_url) in enumerate(episodes, 1):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                log(f"[RSS] ({idx}/{len(episodes)}) 下載: {title}")
                audio = download_file(audio_url, pathlib.Path(tmp), title)
                log(f"[RSS] ({idx}/{len(episodes)}) 轉檔中: {title}")
                res = run_transcription(audio, title, language, batch_size)
                log(f"[RSS] ({idx}/{len(episodes)}) 完成 → {res['txt_file']}")
            except Exception as e:
                log(f"[RSS] ({idx}/{len(episodes)}) 失敗 {title}: {e}")
    log("[RSS] 整季處理結束")


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
</div>
<div class="box">

  <div class="panel active" id="panel-file">
    <label>音檔 (mp3 / m4a / wav / ...)</label>
    <input type="file" id="file" accept="audio/*,video/*">
    <label>語言</label>
    <select id="lang-file">__LANGOPTS__</select>
    <label>批次大小 <span class="hint">越大越快、GPU 越熱</span></label>
    <select id="batch-file">__BATCHOPTS__</select>
    <button onclick="doTranscribe('file')">開始轉文字</button>
  </div>

  <div class="panel" id="panel-url">
    <label>YouTube 或影音 / podcast 單集網址</label>
    <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=...">
    <label>語言</label>
    <select id="lang-url">__LANGOPTS__</select>
    <label>批次大小 <span class="hint">越大越快、GPU 越熱</span></label>
    <select id="batch-url">__BATCHOPTS__</select>
    <button onclick="doTranscribe('url')">下載並轉文字</button>
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
    <button onclick="startFeed()">開始批次處理</button>
    <div id="feedStatus"></div>
  </div>

  <div id="status"></div>
  <textarea id="result" placeholder="單檔/單連結的結果會顯示在這；所有結果都會存到 output/ 資料夾"></textarea>
</div>

<script>
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('panel-' + t.dataset.tab).classList.add('active');
});

const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');

async function doTranscribe(mode) {
  const fd = new FormData();
  fd.append('language', document.getElementById('lang-' + mode).value);
  fd.append('batch_size', document.getElementById('batch-' + mode).value);
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
  statusEl.textContent = '處理中… (時間視長度而定，請勿關閉頁面)';
  const t0 = Date.now();
  try {
    const r = await fetch(url, { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '失敗');
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    statusEl.textContent = `完成 (耗時 ${secs}s) | 語言: ${data.language} | 長度: ${data.duration.toFixed(1)}s | 已存: ${data.txt_file}, ${data.srt_file}`;
    resultEl.value = data.text;
  } catch (e) {
    statusEl.textContent = '錯誤: ' + e.message;
  }
}

let feedTimer = null;
async function startFeed() {
  const feed = document.getElementById('feed').value.trim();
  if (!feed) { document.getElementById('feedStatus').textContent = '請輸入 RSS 網址'; return; }
  const fd = new FormData();
  fd.append('feed_url', feed);
  fd.append('limit', document.getElementById('feedLimit').value || '0');
  fd.append('language', document.getElementById('lang-feed').value);
  fd.append('batch_size', document.getElementById('batch-feed').value);
  const r = await fetch('/transcribe_feed', { method: 'POST', body: fd });
  const data = await r.json();
  document.getElementById('feedStatus').textContent = data.detail || JSON.stringify(data);
  if (feedTimer) clearInterval(feedTimer);
  feedTimer = setInterval(pollJobs, 3000);
  pollJobs();
}
async function pollJobs() {
  const r = await fetch('/jobs');
  const data = await r.json();
  document.getElementById('feedStatus').textContent = data.log.join('\\n') || '(尚無進度)';
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
    return {"status": "ok", "model": MODEL, "device": DEVICE, "batch_size": BATCH_SIZE}


@app.get("/gpu")
def gpu():
    return gpu_stats()


@app.get("/jobs")
def jobs():
    return {"log": JOB_LOG[-40:]}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
):
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = pathlib.Path(tmp) / file.filename
        with open(audio_path, "wb") as fh:
            fh.write(await file.read())
        try:
            return run_transcription(audio_path, file.filename, language, batch_size)
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})


@app.post("/transcribe_url")
def transcribe_url(
    url: str = Form(...),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
):
    with tempfile.TemporaryDirectory() as tmp:
        try:
            audio_path = download_with_ytdlp(url, pathlib.Path(tmp))
            return run_transcription(audio_path, audio_path.name, language, batch_size)
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})


@app.post("/transcribe_feed")
def transcribe_feed(
    background: BackgroundTasks,
    feed_url: str = Form(...),
    limit: int = Form(0),
    language: str = Form(""),
    batch_size: int = Form(BATCH_SIZE),
):
    background.add_task(process_feed, feed_url, limit, language or None, batch_size)
    scope = "整季" if limit == 0 else f"最新 {limit} 集"
    return {"detail": f"已開始背景處理 RSS ({scope}, batch_size={batch_size})，進度顯示在下方，結果存到 output/"}
