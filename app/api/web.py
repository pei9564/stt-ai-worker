"""Web UI：把 templates/index.html 套上執行期設定後回傳。"""

from __future__ import annotations

import pathlib

from app.config import Settings

_TEMPLATE_PATH = pathlib.Path(__file__).parent / "templates" / "index.html"

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


def _batch_opts(batch_size: int) -> str:
    """產生批次大小下拉選單；.env 設定值若不在預設清單中，額外補一個選項。"""
    options = list(_BATCH_CHOICES)
    if batch_size not in {val for val, _ in options}:
        options.insert(0, (batch_size, f"{batch_size} (.env 設定)"))
    return "".join(
        f'<option value="{val}"{" selected" if val == batch_size else ""}>{label}</option>'
        for val, label in options
    )


class WebUI:
    """負責渲染首頁 HTML。模板只在建構時讀一次。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    def render_index(self) -> str:
        s = self._settings
        return (
            self._template.replace("__MODEL__", s.model)
            .replace("__DEVICE__", s.device)
            .replace("__BATCH__", str(s.batch_size))
            .replace("__LANGOPTS__", _LANG_OPTS)
            .replace("__BATCHOPTS__", _batch_opts(s.batch_size))
        )
