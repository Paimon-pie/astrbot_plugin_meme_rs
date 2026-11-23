from __future__ import annotations

import asyncio
import base64
import time
from typing import Any
from urllib.parse import quote

import httpx


class MemeClientError(Exception):
    """Base error for meme-generator-rs API calls."""


class MemeConnectionError(MemeClientError):
    """Raised when the meme-generator-rs server cannot be reached."""


class MemeServerError(MemeClientError):
    """Raised when the meme-generator-rs server returns a non-2xx response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MemeRsClient:
    """Async HTTP client for meme-generator-rs."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:2255",
        timeout: float = 30.0,
        retry: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = max(float(timeout), 3.0)
        self.retry = max(int(retry), 0)
        self._client: httpx.AsyncClient | None = None
        self._download_client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"User-Agent": "astrbot_plugin_meme_rs/1.0.0"},
            )
        return self._client

    def _get_download_client(self) -> httpx.AsyncClient:
        if self._download_client is None or self._download_client.is_closed:
            self._download_client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
            )
        return self._download_client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        if self._download_client is not None and not self._download_client.is_closed:
            await self._download_client.aclose()

    @staticmethod
    def _extract_error_message(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                for field in ("message", "detail", "error", "msg"):
                    if data.get(field):
                        return str(data[field])
                return str(data)
            if isinstance(data, str) and data.strip():
                return data.strip()
        except Exception:
            pass
        text = resp.text.strip()
        return text if text else f"HTTP {resp.status_code}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = self._get_client()
        last_exc: Exception | None = None

        for attempt in range(self.retry + 1):
            try:
                resp = await client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = exc
                if attempt < self.retry:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise MemeConnectionError(
                    f"无法连接到 meme-generator-rs 服务 ({self.base_url})，请确认服务端已启动且地址配置正确。"
                ) from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self.retry:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise MemeConnectionError(
                    f"请求 meme-generator-rs 服务超时 ({self.base_url})。"
                ) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.retry:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise MemeConnectionError(f"请求 meme 服务网络异常: {exc}") from exc

            if resp.status_code >= 500 and attempt < self.retry:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue

            if resp.status_code >= 400:
                msg = self._extract_error_message(resp)
                raise MemeServerError(msg, status_code=resp.status_code)

            return resp

        raise MemeConnectionError(f"请求 meme 服务失败: {last_exc}")

    async def health_check(self) -> tuple[bool, float, int | str]:
        start = time.perf_counter()
        try:
            resp = await self._request("GET", "/meme/keys")
            keys = resp.json()
            elapsed_ms = (time.perf_counter() - start) * 1000
            return True, elapsed_ms, len(keys) if isinstance(keys, list) else 0
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return False, elapsed_ms, str(exc)

    async def fetch_infos(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/meme/infos")
        data = resp.json()
        if not isinstance(data, list):
            raise MemeServerError("服务端返回的 /meme/infos 格式异常")
        return data

    async def fetch_info(self, key: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/memes/{quote(key, safe='')}/info")
        data = resp.json()
        if not isinstance(data, dict):
            raise MemeServerError(f"获取表情 {key} 信息格式异常")
        return data

    async def search(self, query: str) -> list[str]:
        resp = await self._request("GET", "/meme/search", params={"query": query})
        data = resp.json()
        return [str(item) for item in data] if isinstance(data, list) else []

    async def fetch_image(self, image_id: str) -> bytes:
        resp = await self._request("GET", f"/image/{quote(image_id, safe='')}")
        if not resp.content:
            raise MemeServerError("获取生成图片失败：服务端返回空内容")
        return resp.content

    async def fetch_preview(self, key: str) -> bytes:
        resp = await self._request("GET", f"/memes/{quote(key, safe='')}/preview")
        data = resp.json()
        image_id = data.get("image_id") if isinstance(data, dict) else None
        if not image_id:
            raise MemeServerError("获取预览图失败：未返回 image_id")
        return await self.fetch_image(str(image_id))

    async def render_list(self, payload: dict[str, Any] | None = None) -> bytes:
        resp = await self._request("POST", "/tools/render_list", json_body=payload or {})
        data = resp.json()
        image_id = data.get("image_id") if isinstance(data, dict) else None
        if not image_id:
            raise MemeServerError("渲染表情列表失败：未返回 image_id")
        return await self.fetch_image(str(image_id))

    async def upload_image_bytes(self, image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        resp = await self._request(
            "POST",
            "/image/upload",
            json_body={"type": "data", "data": b64},
        )
        data = resp.json()
        image_id = data.get("image_id") if isinstance(data, dict) else None
        if not image_id:
            raise MemeServerError("上传图片失败：未返回 image_id")
        return str(image_id)

    async def upload_image_url(
        self, url: str, headers: dict[str, str] | None = None
    ) -> str:
        body: dict[str, Any] = {"type": "url", "url": url}
        if headers:
            body["headers"] = headers
        resp = await self._request("POST", "/image/upload", json_body=body)
        data = resp.json()
        image_id = data.get("image_id") if isinstance(data, dict) else None
        if not image_id:
            raise MemeServerError("上传图片 URL 失败：未返回 image_id")
        return str(image_id)

    async def generate_meme(
        self,
        key: str,
        images: list[dict[str, str]],
        texts: list[str],
        options: dict[str, Any],
    ) -> bytes:
        payload = {
            "images": images,
            "texts": texts,
            "options": options,
        }
        resp = await self._request(
            "POST",
            f"/memes/{quote(key, safe='')}",
            json_body=payload,
        )
        data = resp.json()
        image_id = data.get("image_id") if isinstance(data, dict) else None
        if not image_id:
            raise MemeServerError("生成表情失败：未返回 image_id")
        return await self.fetch_image(str(image_id))

    async def download_image_bytes(self, url: str) -> bytes:
        client = self._get_download_client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            if not resp.content:
                raise MemeClientError(f"下载图片为空: {url}")
            return resp.content
        except httpx.HTTPError as exc:
            raise MemeClientError(f"下载图片失败 ({url}): {exc}") from exc
