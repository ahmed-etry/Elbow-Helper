"""Search GIPHY and download bounded candidate still images."""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GifCandidate:
    url: str
    still_url: str | None


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
        candidates = await self.search_candidates(query, limit=1)
        return candidates[0].url if candidates else None

    async def search_candidates(self, query, *, limit=6) -> list[GifCandidate]:
        if not self.configured or not isinstance(query, str) or not query.strip():
            return []
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.giphy.com/v1/gifs/search",
                params={
                    "api_key": self._api_key,
                    "q": query,
                    "limit": limit,
                    "rating": "pg-13",
                    "lang": "en",
                },
            ) as response:
                if response.status != 200:
                    return []
                payload = await response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            candidates = []
            for item in data[:limit] if isinstance(data, list) else ():
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if (not isinstance(url, str) or urlsplit(url).scheme != "https"
                        or urlsplit(url).hostname not in {"giphy.com", "www.giphy.com"}):
                    continue
                images = item.get("images") or {}
                still = (images.get("original_still") or {}).get("url")
                candidates.append(GifCandidate(url, still))
            return candidates
        except (aiohttp.ClientError, TimeoutError, ValueError):
            LOGGER.warning("GIF search is unavailable")
        return []

    async def download_still(self, url):
        if not isinstance(url, str):
            return None
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or not host.endswith(".giphy.com"):
            return None
        session = await self._get_session()
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                return None
            data = bytearray()
            async for part in response.content.iter_chunked(64 * 1024):
                data.extend(part)
                if len(data) > 8 * 1024 * 1024:
                    return None
            return bytes(data)
