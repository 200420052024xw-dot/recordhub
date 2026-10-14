"""进程内 TTL 缓存：避免翻页或重复查询时反复全表读飞书。

单进程应用（见 README），一个带锁的字典足够；锁覆盖 TestClient / uvicorn 线程。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any


class TtlCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Any]] = {}

    def get_or_load(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[0] < self.ttl_seconds:
                return entry[1]
        value = loader()
        with self._lock:
            self._entries[key] = (time.monotonic(), value)
        return value
