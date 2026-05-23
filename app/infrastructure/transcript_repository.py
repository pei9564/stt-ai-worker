"""Transcript (轉檔成品) 的儲存實作。

- InMemoryTranscriptRepository:測試用,以遞增整數當 id。
- PostgresTranscriptRepository:正式環境,寫進 PG `transcripts` 表。
"""

from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool


class InMemoryTranscriptRepository:
    """純記憶體儲存。id 從 1 遞增。"""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def save(
        self,
        *,
        topic: str,
        text: str,
        srt: str,
        language: str,
        duration: float,
    ) -> int:
        with self._lock:
            tid = self._next_id
            self._next_id += 1
            self._rows[tid] = {
                "topic": topic,
                "text": text,
                "srt": srt,
                "language": language,
                "duration": duration,
            }
            return tid

    def delete(self, transcript_id: int) -> None:
        with self._lock:
            self._rows.pop(transcript_id, None)

    # 測試輔助
    def get(self, transcript_id: int) -> dict | None:
        return self._rows.get(transcript_id)


class PostgresTranscriptRepository:
    """寫入 PG `transcripts` 表。共用 ConnectionPool。"""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def save(
        self,
        *,
        topic: str,
        text: str,
        srt: str,
        language: str,
        duration: float,
    ) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transcripts (topic, language, duration, text, srt)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (topic, language or None, duration, text, srt),
            )
            (tid,) = cur.fetchone()
            return tid

    def delete(self, transcript_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM transcripts WHERE id = %s", (transcript_id,))
