-- stt-ai-worker 的 PG schema。
--
-- 設計成 idempotent (CREATE TABLE IF NOT EXISTS),服務啟動時跑一次。
-- v0.1.0 上線前都靠這支處理 schema 演進;之後再導入 alembic。
--
-- 兩張表:
--   transcripts —— 一次轉檔的成品(文字 + SRT),standalone 使用者直接看這張。
--   jobs        —— 一次轉檔請求的生命週期狀態,完成時 FK 到 transcripts。

CREATE TABLE IF NOT EXISTS transcripts (
    id          BIGSERIAL PRIMARY KEY,
    topic       TEXT NOT NULL,
    language    TEXT,
    duration    REAL,
    text        TEXT NOT NULL,
    srt         TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    topic           TEXT,
    language        TEXT,
    batch_size      INT  NOT NULL,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    transcript_id   BIGINT REFERENCES transcripts(id) ON DELETE SET NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC);

-- 向後相容：舊 jobs 保留，缺少來源的未完成任務會標記 error。
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source JSONB;
