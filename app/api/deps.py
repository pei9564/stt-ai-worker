"""FastAPI 相依注入：從 app.state 取出 Container。"""

from __future__ import annotations

from fastapi import Request

from app.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
