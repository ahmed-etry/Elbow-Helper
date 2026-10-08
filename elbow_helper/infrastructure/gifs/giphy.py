"""Return GIPHY page links without downloading GIF media."""

import asyncio
import logging
from urllib.parse import urlsplit

import aiohttp

LOGGER = logging.getLogger(__name__)


class GiphyClient:
    def __init__(self, api_key):
        self._api_key = str(api_key or "").strip() or None
        self._session = None
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return self._api_key is not None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def close(self):
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def _get_session(self):
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            return self._session

    async def search(self, query) -> str | None:
        if not self.configured or not isinstance(query, str) or not query.strip():
            return None
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.giphy.com/v1/gifs/search",
                params={
                    "api_key": self._api_key,
                    "q": query,
                    "limit": 1,
                    "rating": "pg-13",
                    "lang": "en",
                },
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            url = (
                data[0].get("url")
                if isinstance(data, list) and data and isinstance(data[0], dict) else None
            )
            if (
                isinstance(url, str) and urlsplit(url).scheme == "https"
                and urlsplit(url).hostname in {"giphy.com", "www.giphy.com"}
            ):
                return url
        except (aiohttp.ClientError, TimeoutError, ValueError):
            LOGGER.warning("GIF search is unavailable")
        return None
