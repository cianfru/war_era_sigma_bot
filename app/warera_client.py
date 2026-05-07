"""Async httpx-based tRPC client for the Warera API.

tRPC v10 wire format:
- Single query:  POST /trpc/<procedure>      body={"input": {"json": <payload>}}
- Batch query:   POST /trpc/<a>,<b>?batch=1  body={"0": {"json": <p1>}, "1": {"json": <p2>}}
- Response data is wrapped under result.data.json (single) or [{result: {data: {json}}}, ...] (batch).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class WareraAPIError(RuntimeError):
    pass


class WareraClient:
    def __init__(
        self,
        base_url: str = "https://api2.warera.io/trpc",
        api_key: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "warera-monitor/1.0",
        }
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WareraClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                logger.warning(
                    "warera request failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
        raise WareraAPIError(f"request failed after retries: {last_exc}") from last_exc

    async def query(self, procedure: str, payload: dict[str, Any] | None = None) -> Any:
        body = {"input": {"json": payload if payload is not None else {}}}
        resp = await self._request("POST", f"/{procedure}", content=json.dumps(body))
        data = resp.json()
        return _unwrap_single(data)

    async def batch_query(self, calls: list[tuple[str, dict[str, Any] | None]]) -> list[Any]:
        if not calls:
            return []
        procedures = ",".join(p for p, _ in calls)
        body = {
            str(i): {"json": payload if payload is not None else {}}
            for i, (_, payload) in enumerate(calls)
        }
        resp = await self._request(
            "POST", f"/{procedures}?batch=1", content=json.dumps(body)
        )
        data = resp.json()
        if not isinstance(data, list):
            raise WareraAPIError(f"expected batch list response, got: {type(data)}")
        return [_unwrap_single(entry) for entry in data]

    async def get_prices(self) -> dict[str, Any]:
        result = await self.query("itemTrading.getPrices", {})
        if not isinstance(result, dict):
            raise WareraAPIError(f"unexpected getPrices payload: {type(result)}")
        return result

    async def get_top_orders(self, item_code: str, limit: int = 10) -> Any:
        return await self.query(
            "itemTrading.getTopOrders", {"itemCode": item_code, "limit": limit}
        )


def _unwrap_single(payload: Any) -> Any:
    """Unwrap a tRPC v10 result envelope and return the inner JSON value."""
    if isinstance(payload, dict):
        if "error" in payload and payload["error"]:
            raise WareraAPIError(f"tRPC error: {payload['error']}")
        result = payload.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict) and "json" in data:
                return data["json"]
            return data
    return payload


def normalize_prices(raw: dict[str, Any]) -> dict[str, float]:
    """Best-effort normalization of getPrices output to {item_code: price}.

    The exact response shape is observed at runtime; we accept either:
      {"iron": 12.5, ...}
      {"iron": {"price": 12.5, ...}, ...}
      {"iron": {"avgPrice": 12.5, ...}, ...}
    """
    out: dict[str, float] = {}
    for code, value in raw.items():
        price: float | None = None
        if isinstance(value, (int, float)):
            price = float(value)
        elif isinstance(value, str):
            try:
                price = float(value)
            except ValueError:
                price = None
        elif isinstance(value, dict):
            for key in ("price", "avgPrice", "averagePrice", "currentPrice", "lastPrice"):
                v = value.get(key)
                if isinstance(v, (int, float)):
                    price = float(v)
                    break
                if isinstance(v, str):
                    try:
                        price = float(v)
                        break
                    except ValueError:
                        continue
        if price is not None and price > 0:
            out[code] = price
    return out
