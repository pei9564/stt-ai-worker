"""Resource limits shared by network acquisition and audio preflight."""
import math
import os
from pathlib import Path
import shutil
import subprocess


def positive_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


def require_disk(path: Path, extra: int = 0) -> None:
    minimum = positive_env('MIN_FREE_DISK_BYTES', 512 * 1024 * 1024)
    if shutil.disk_usage(path).free < minimum + extra:
        raise RuntimeError('磁碟可用空間不足，請清理音檔後重試')


def probe_duration(path: Path) -> float:
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                            capture_output=True, text=True, timeout=30)
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError('無法讀取音訊長度，請確認檔案可播放') from exc
    if result.returncode or not math.isfinite(duration) or duration <= 0:
        raise ValueError('音訊格式無效或無法讀取長度')
    return duration


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        size = 0

        async def bounded_receive():
            nonlocal size
            from starlette.exceptions import HTTPException
            message = await receive()
            size += len(message.get('body', b''))
            if size > self.max_bytes:
                raise HTTPException(413, '上傳超過大小限制')
            return message

        await self.app(scope, bounded_receive, send)
