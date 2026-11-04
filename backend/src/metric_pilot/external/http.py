"""有界只读 HTTP：连接固定到已校验公网 IP，拒绝跳转，取消关闭实际流。"""

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpcore
import httpx

from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.external.registry import NCEI_ROOT, PUBLIC_SOURCE_HOSTS, validate_url


class PublicNetworkBackend(httpcore.AnyIOBackend):
    """
    作用：DNS 校验后直连选定公网 IP，TLS 仍由 httpcore 校验原主机名。
    输入：连接目标。
    输出：异步网络流；阻止内网、回环和 DNS 重新解析绕过。
    """

    def __init__(self, dns_mode="system", audit=None):
        """
        作用：选择系统 DNS 或操作者显式选择的固定公网加密 DNS。
        输入：解析方式、抓取审计列表。
        输出：无。
        """
        self.dns_mode = dns_mode
        self.audit = audit if audit is not None else []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        """
        作用：校验全部 DNS 答案并连接其中一个公网地址。
        输入：主机、端口及连接预算。
        输出：实际公网连接。
        """
        if host not in PUBLIC_SOURCE_HOSTS or port != 443:
            raise DataError("EXTERNAL_ADDRESS_REJECTED")
        if self.dns_mode == "cloudflare":
            # 显式选项仅向固定公网 IP 发域名；TLS 校验证书，不使用代理或重定向。
            async with httpx.AsyncClient(
                timeout=10, trust_env=False, follow_redirects=False
            ) as dns:
                async with dns.stream(
                    "GET",
                    "https://1.1.1.1/dns-query",
                    params={"name": host, "type": "A"},
                    headers={
                        "Accept": "application/dns-json",
                        "User-Agent": "BusinessAwareAnalysis-weather/0.1",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise DataError("EXTERNAL_DNS_FAILED")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 65536:
                            raise DataError("EXTERNAL_DNS_FAILED")
                        raw.extend(chunk)
            try:
                answer = json.loads(raw)
                if not isinstance(answer, dict) or answer.get("Status") != 0 or answer.get("TC"):
                    raise DataError("EXTERNAL_DNS_FAILED")
                if not isinstance(answer.get("Answer", []), list):
                    raise DataError("EXTERNAL_DNS_FAILED")
                addresses = [
                    item["data"]
                    for item in answer.get("Answer", [])
                    if isinstance(item, dict) and item.get("type") == 1
                ]
            except (ValueError, TypeError, KeyError):
                raise DataError("EXTERNAL_DNS_FAILED") from None
        elif self.dns_mode == "system":
            resolved = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
            addresses = [item[4][0] for item in resolved]
        else:
            raise DataError("EXTERNAL_DNS_FAILED")
        self.audit.append(dict(host=host, mode=self.dns_mode, addresses=addresses))
        try:
            public = bool(addresses) and all(ipaddress.ip_address(ip).is_global for ip in addresses)
        except ValueError:
            public = False
        if not public:
            raise DataError("EXTERNAL_ADDRESS_REJECTED")
        # 传递数字 IP，避免校验域名后由连接库再次解析；不使用环境代理。
        address = addresses[0]
        return await super().connect_tcp(
            address, port, timeout=timeout, local_address=None, socket_options=socket_options
        )


class SourceHTTP:
    """
    作用：限制请求、读取、时间与原件保存；失败原件不作为成功证据。
    输入：存储目录、最大请求数、期限和可选隔离连接池。
    输出：完整响应字节及抓取清单。
    """

    def __init__(
        self,
        storage: Path,
        *,
        timeout=120,
        max_requests=8,
        pool_factory=None,
        dns_mode="system",
        max_total_bytes=None,
        historical_cache=False,
    ):
        """
        作用：初始化单次工具查询的网络预算。
        输入：存储目录、总秒数、次数上限及测试工厂。
        输出：无。
        """
        self.root = storage / "external" / "fetches"
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests = 0
        self.started = time.monotonic()
        self.last_request = 0.0
        self.records = []
        self.dns_mode = dns_mode
        self.dns_audit = []
        self.bytes_received = 0
        self.max_total_bytes = max_total_bytes
        self.historical_cache = historical_cache
        self.cache_hits = set()
        self.revalidations = []
        self.pool_factory = pool_factory or (
            lambda: httpcore.AsyncConnectionPool(
                network_backend=PublicNetworkBackend(dns_mode, self.dns_audit),
                max_connections=1,
                max_keepalive_connections=0,
                retries=0,
            )
        )

    def cached(self, url, params, max_bytes, max_age, *, conditional=False):
        """
        作用：核对历史原件；过期时只提供条件请求候选，不直接授权复用。
        输入：固定来源条件、上限、缓存期限及是否寻找过期验证候选。
        输出：短期复用结果或待官方核验的原件；保留原获取时间。
        """
        if (
            not self.historical_cache
            or not max_age
            or not url.startswith(NCEI_ROOT + "/hourly/access/by-year/")
        ):
            return None
        paths = sorted(
            self.root.glob("fetch_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:500]
        for path in paths:
            try:
                if path.stat().st_size > 65536:
                    continue
                record = json.loads(path.read_bytes())
                if (
                    record.get("url") != url
                    or record.get("params") != (params or {})
                    or record.get("status") != "succeeded"
                    or record.get("truncated")
                    or record.get("http_status") != 200
                    or not re.fullmatch(r"fetch_[a-f0-9]{32}", record.get("id", ""))
                ):
                    continue
                fetched = datetime.fromisoformat(record["fetched_at"])
                if fetched.tzinfo is None:
                    continue
                age = (datetime.now(UTC) - fetched).total_seconds()
                if age < 0:
                    continue
                if conditional:
                    if age <= min(max_age, 21600) or not isinstance(record.get("etag"), str):
                        continue
                    if len(record["etag"]) > 512 or not re.fullmatch(
                        r'(?:W/)?"[^"\r\n]*"', record["etag"]
                    ):
                        continue
                elif age > min(max_age, 21600):
                    continue
                raw_path = self.root / (record["id"] + ".raw")
                if (
                    not raw_path.resolve().is_relative_to(self.root.resolve())
                    or raw_path.stat().st_size > max_bytes
                ):
                    continue
                raw = raw_path.read_bytes()
                if (
                    len(raw) != record["bytes"]
                    or hashlib.sha256(raw).hexdigest() != record["sha256"]
                ):
                    continue
                return (raw, record) if conditional else self.reuse(raw, record)
            except DataError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def reuse(self, raw, record):
        """
        作用：经校验的原件复用仍计入字节预算，保留真实原获取时间。
        输入：完整原件与原记录。
        输出：原件及记录。
        """
        if (
            self.max_total_bytes is not None
            and self.bytes_received + len(raw) > self.max_total_bytes
        ):
            raise DataError("EXTERNAL_BYTE_BUDGET_EXCEEDED")
        self.bytes_received += len(raw)
        self.cache_hits.add(len(self.records))
        self.records.append(record)
        return raw, record

    async def get(
        self, url: str, params: dict | None = None, *, max_bytes=2_000_000, cache_max_age=0
    ):
        """
        作用：流式读取单个白名单响应，取消/超时退出连接上下文并留失败记录。
        输入：固定来源 URL、服务端参数及保存字节上限。
        输出：完整字节与清单；任何截断均抛错。
        """
        validate_url(url)
        if self.timeout - (time.monotonic() - self.started) <= 0:
            raise DataError("EXTERNAL_TIMEOUT")
        cached = self.cached(url, params, max_bytes, cache_max_age)
        if cached is not None:
            return cached
        candidate = self.cached(url, params, max_bytes, cache_max_age, conditional=True)
        if self.requests >= self.max_requests:
            raise DataError("EXTERNAL_REQUEST_LIMIT")
        self.requests += 1
        remaining = self.timeout - (time.monotonic() - self.started)
        if remaining <= 0:
            raise DataError("EXTERNAL_TIMEOUT")
        fetch_id = new_id("fetch")
        self.root.mkdir(parents=True, exist_ok=True)
        record = dict(
            id=fetch_id,
            url=url,
            params=params or {},
            fetched_at=utc_now(),
            status="failed",
            truncated=True,
            http_status=None,
            retry_policy="no_retry",
        )
        dns_offset = len(self.dns_audit)
        body = bytearray()
        query_url = url + ("?" + urlencode(params) if params else "")
        try:
            async with asyncio.timeout(remaining):
                # 比官方公布的 AWC 配额更保守；服务失败不立即重复请求。
                await asyncio.sleep(max(0, 2 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                async with self.pool_factory() as pool:
                    async with pool.stream(
                        "GET",
                        query_url,
                        headers={
                            "User-Agent": "BusinessAwareAnalysis-external/0.1",
                            "Accept-Encoding": "identity",
                            "Accept": "*/*",
                            **({"If-None-Match": candidate[1]["etag"]} if candidate else {}),
                        },
                        extensions={"timeout": dict(connect=15, read=25, write=10, pool=5)},
                    ) as response:
                        headers = {
                            k.decode().lower(): v.decode("latin-1") for k, v in response.headers
                        }
                        record.update(
                            http_status=response.status,
                            content_type=headers.get("content-type"),
                            etag=headers.get("etag"),
                            last_modified=headers.get("last-modified"),
                            retry_after=headers.get("retry-after"),
                        )
                        if response.status == 304 and candidate:
                            # 304只是版本核验，不冒充本次重新下载原件。
                            reused = self.reuse(*candidate)
                            record.update(
                                status="succeeded",
                                truncated=False,
                                validation_only=True,
                                validated_fetch_id=candidate[1]["id"],
                                if_none_match=candidate[1]["etag"],
                            )
                            self.revalidations.append(record)
                            return reused
                        if 300 <= response.status < 400:
                            raise DataError("EXTERNAL_REDIRECT_REJECTED")
                        if response.status not in (200, 204):
                            raise DataError("EXTERNAL_HTTP_ERROR", {"status": response.status})
                        # 不接受压缩内容，避免压缩炸弹；不跟随服务端跳转。
                        if headers.get("content-encoding", "identity").lower() != "identity":
                            raise DataError("EXTERNAL_ENCODING_REJECTED")
                        async for chunk in response.aiter_stream():
                            self.bytes_received += len(chunk)
                            if (
                                self.max_total_bytes is not None
                                and self.bytes_received > self.max_total_bytes
                            ):
                                raise DataError("EXTERNAL_BYTE_BUDGET_EXCEEDED")
                            available = max_bytes - len(body)
                            body.extend(chunk[:available])
                            if len(chunk) > available:
                                raise DataError("EXTERNAL_RESPONSE_LIMIT")
                        record.update(status="succeeded", truncated=False)
        except asyncio.CancelledError:
            record["error_code"] = "EXTERNAL_CANCELLED"
            raise
        except (TimeoutError, httpcore.TimeoutException, httpx.TimeoutException):
            record["error_code"] = "EXTERNAL_TIMEOUT"
            raise DataError("EXTERNAL_TIMEOUT") from None
        except (httpcore.NetworkError, httpcore.ProtocolError, httpx.HTTPError, OSError):
            record["error_code"] = "EXTERNAL_CONNECTION_FAILED"
            raise DataError("EXTERNAL_CONNECTION_FAILED") from None
        except DataError as error:
            record["error_code"] = error.code
            raise
        finally:
            # 即使失败或中途取消，也保存已经读取的原件并明确标成不完整。
            raw = bytes(body)
            record.update(
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                finished_at=utc_now(),
                dns=list(self.dns_audit[dns_offset:]),
            )
            (self.root / f"{fetch_id}.raw").write_bytes(raw)
            (self.root / f"{fetch_id}.json").write_bytes(canonical_bytes(record))
            self.records.append(record)
        return bytes(body), record
