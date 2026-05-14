"""正式啟動進入點：`uvicorn app.asgi:app`。

模組載入時就會建立 app (並載入 Whisper 模型)，因此測試請改用
`app.main.create_app(...)` 並注入假的 transcriber。
"""

from app.main import create_app

app = create_app()
