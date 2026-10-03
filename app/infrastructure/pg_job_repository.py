"""PostgresJobRepository — Job 生命週期紀錄,LEFT JOIN transcripts 重建 JobResult。

並行語意:JobService 已用 lock 序列化 repo 存取,本層的 SQL 不再額外加鎖。
"""

from __future__ import annotations

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.domain.job import Job, JobResult, JobStatus, JobType
from app.infrastructure.audio_sources import deserialize_source, serialize_source

# SELECT 預設帶 LEFT JOIN,完成的 job 一次撈出 transcript 內容
_SELECT_COLS = """
    j.id, j.type, j.topic, j.language, j.batch_size, j.status,
    j.created_at, j.started_at, j.finished_at,
    j.transcript_id, j.error,
    t.language, t.duration, t.text, t.srt, j.source
"""


def _row_to_job(row: tuple) -> Job:
    (
        jid, jtype, topic, language, batch_size, status,
        created_at, started_at, finished_at,
        transcript_id, error,
        t_language, t_duration, t_text, t_srt, source,
    ) = row

    result: JobResult | None = None
    if transcript_id is not None and t_text is not None:
        result = JobResult(
            language=t_language or "",
            duration=t_duration or 0.0,
            text=t_text,
            srt=t_srt or "",
            transcript_id=transcript_id,
        )

    return Job(
        id=jid,
        type=JobType(jtype),
        topic=topic,
        language=language,
        batch_size=batch_size,
        status=JobStatus(status),
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        result=result,
        error=error,
        source=deserialize_source(source),
    )


class PostgresJobRepository:
    """寫入 PG `jobs` 表;result 透過 transcripts FK 重建。"""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def save(self, job: Job) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            self._save(cur, job)

    def save_many(self, jobs: list[Job]) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            for job in jobs:
                self._save(cur, job)

    def _save(self, cur, job: Job) -> None:
        tid = job.result.transcript_id if job.result else None
        source = serialize_source(job.source)
        cur.execute(
            """
            INSERT INTO jobs (
                id, type, topic, language, batch_size, status,
                created_at, started_at, finished_at,
                transcript_id, error, source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status        = EXCLUDED.status,
                started_at    = EXCLUDED.started_at,
                finished_at   = EXCLUDED.finished_at,
                transcript_id = EXCLUDED.transcript_id,
                error         = EXCLUDED.error,
                source        = EXCLUDED.source
            """,
            (
                job.id,
                job.type.value,
                job.topic,
                job.language,
                job.batch_size,
                job.status.value,
                job.created_at,
                job.started_at,
                job.finished_at,
                tid,
                job.error,
                Jsonb(source) if source is not None else None,
            ),
        )

    def get(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SELECT_COLS}
                FROM jobs j
                LEFT JOIN transcripts t ON t.id = j.transcript_id
                WHERE j.id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return _row_to_job(row) if row else None

    def all(self) -> list[Job]:
        """所有 job,依建立順序由舊到新。

        JobService 會用 list_recent() 套時間窗,所以這裡可以全表撈;之後若資料量
        變大,改成在 SQL 層用 created_at >= cutoff 過濾。
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SELECT_COLS}
                FROM jobs j
                LEFT JOIN transcripts t ON t.id = j.transcript_id
                ORDER BY j.created_at ASC
                """
            )
            return [_row_to_job(row) for row in cur.fetchall()]

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))


