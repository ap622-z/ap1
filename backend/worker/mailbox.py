"""Mailbox：同一会话同一时刻至多处理一条消息。

进程内实现，按 session_id 键控；跨会话并行、同会话串行。
接口形态预留可替换为分布式实现（演进接缝）。
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class Mailbox:
    """进程内 mailbox：为每个会话维护一把 asyncio.Lock。

    锁对象随会话自动回收（weakref），避免长会话泄漏。
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._guard = asyncio.Lock()

    async def _get_lock(self, session_id: int) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    @asynccontextmanager
    async def run(self, session_id: int) -> AsyncIterator[None]:
        """以会话为粒度临界区：拿到锁期间该会话至多一条消息在途。"""
        lock = await self._get_lock(session_id)
        async with lock:
            yield
