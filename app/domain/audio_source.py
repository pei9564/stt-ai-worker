"""音檔來源的抽象介面。

新增一種來源 (例如雲端硬碟連結) 只需在 infrastructure 實作一個子類別，
其餘流程 (排隊、轉檔、輸出) 完全不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AudioSource(ABC):
    """一個能提供本地音檔路徑的來源。

    `acquire()` 是 context manager：進入時保證有一個可讀的本地音檔，
    離開時負責清掉暫存 (下載的檔案、上傳的暫存檔等)。
    """

    @property
    @abstractmethod
    def display_name(self) -> str:
        """供輸出檔名 / 記錄使用的人類可讀名稱。"""

    @abstractmethod
    @contextmanager
    def acquire(self) -> Iterator[Path]:
        """yield 一個本地音檔路徑；離開 context 時清理暫存。"""
        raise NotImplementedError
        yield  # pragma: no cover  (讓型別檢查器知道這是 generator)
