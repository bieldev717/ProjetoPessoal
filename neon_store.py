"""Acesso PostgreSQL assíncrono ao Neon para as rotas Flask síncronas."""

import asyncio
import threading
from concurrent.futures import Future
from typing import Awaitable, Callable, TypeVar

import asyncpg

T = TypeVar("T")


class NeonStore:
    def __init__(self, dsn: str):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="neon-asyncpg-loop")
        self._thread.start()
        self._pool = self._submit(self._create_pool(dsn))

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, awaitable: Awaitable[T]) -> T:
        future: Future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(timeout=20)

    async def _create_pool(self, dsn: str):
        return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, command_timeout=20)

    async def _fetch(self, query: str, args: tuple):
        return await self._pool.fetch(query, *args)

    async def _fetchrow(self, query: str, args: tuple):
        return await self._pool.fetchrow(query, *args)

    async def _execute(self, query: str, args: tuple):
        return await self._pool.execute(query, *args)

    def fetch(self, query: str, *args):
        return self._submit(self._fetch(query, args))

    def fetchrow(self, query: str, *args):
        return self._submit(self._fetchrow(query, args))

    def execute(self, query: str, *args):
        return self._submit(self._execute(query, args))

    def transaction(self, operation: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
        async def run():
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    return await operation(connection)

        return self._submit(run())

    def close(self):
        if self._loop.is_closed():
            return
        try:
            self._submit(self._pool.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
