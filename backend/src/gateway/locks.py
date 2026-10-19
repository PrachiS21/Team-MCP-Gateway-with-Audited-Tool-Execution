"""One async lock per resource key, so two operations on the same resource never overlap.

Used by GatewayService (around compensable calls) and by the undo worker, which is
what keeps an undo from racing with other requests. Unrelated keys do not block each
other. Single-process only: with several gateway replicas this must become a database
lease (noted as a limit).
"""

import asyncio
from contextlib import asynccontextmanager


class KeyedLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if self._users[key] == 0:  # nobody waiting or holding: forget the lock
                del self._users[key]
                del self._locks[key]
