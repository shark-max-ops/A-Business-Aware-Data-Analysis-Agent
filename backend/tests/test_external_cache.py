"""历史公开原件缓存：复用成功响应，不复用旧运行查询或伪造获取时间。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.external.http import SourceHTTP
from metric_pilot.external.registry import NCEI_ROOT
from test_external_weather import URL as AWC_URL
from test_external_weather import Pool

URL = NCEI_ROOT + "/hourly/access/by-year/2026/psv/GHCNh_USW00013874_2026.psv"


@pytest.mark.parametrize("response_status", [304, 200])
def test_expired_original_requires_actual_conditional_response(tmp_path, response_status):
    """
    作用：过期原件必须真实核验版本；304复用旧完整原件，200保存新响应，不伪造下载时间。
    输入：带ETag的明确合成历史响应。
    输出：无。
    """

    async def verify():
        """作用：比较真实服务记录；输入：无；输出：无。"""
        first = SourceHTTP(
            tmp_path,
            pool_factory=lambda: Pool((b"synthetic old",), headers=[(b"ETag", b'"synthetic"')]),
        )
        old_raw, old = await first.get(URL)
        old["fetched_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        (first.root / (old["id"] + ".json")).write_bytes(canonical_bytes(old))
        pool = Pool(
            () if response_status == 304 else (b"synthetic changed",), status=response_status
        )
        client = SourceHTTP(tmp_path, historical_cache=True, pool_factory=lambda: pool)
        raw, result = await client.get(URL, cache_max_age=21600)
        assert client.requests == 1 and pool.closed
        if response_status == 304:
            assert raw == old_raw and result == old
            validation = client.revalidations[0]
            assert (
                validation["http_status"] == 304 and validation["validated_fetch_id"] == old["id"]
            )
            assert validation["bytes"] == 0 and validation["if_none_match"] == old["etag"]
            assert (
                json.loads((client.root / (validation["id"] + ".json")).read_bytes()) == validation
            )
            assert client.bytes_received == len(raw)
        else:
            assert raw == b"synthetic changed" and result["id"] != old["id"]
            assert not client.revalidations and not client.cache_hits
        assert json.loads((client.root / (old["id"] + ".json")).read_bytes()) == old

    asyncio.run(verify())


@pytest.mark.parametrize("change", ["tampered", "no_validator", "failed"])
def test_unrequested_304_cannot_validate_invalid_original(tmp_path, change):
    """作用：坏原件或缺少验证器时不能接受304；输入：合成坏缓存；输出：无。"""

    async def verify():
        """作用：检查来源拒绝；输入：无；输出：无。"""
        first = SourceHTTP(
            tmp_path,
            pool_factory=lambda: Pool((b"synthetic old",), headers=[(b"etag", b'"synthetic"')]),
        )
        _, old = await first.get(URL)
        old["fetched_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        if change == "tampered":
            (first.root / (old["id"] + ".raw")).write_bytes(b"tampered")
        elif change == "no_validator":
            old.pop("etag")
        else:
            old["status"] = "failed"
        (first.root / (old["id"] + ".json")).write_bytes(canonical_bytes(old))
        client = SourceHTTP(tmp_path, historical_cache=True, pool_factory=lambda: Pool(status=304))
        with pytest.raises(DataError):
            await client.get(URL, cache_max_age=21600)
        assert not client.revalidations and not client.cache_hits

    asyncio.run(verify())


def test_conditional_reuse_budget_failure_is_not_success(tmp_path):
    """作用：304确认后字节预算不足仍记录失败并关闭连接；输入：合成原件；输出：无。"""

    async def verify():
        """作用：执行有界复用失败；输入：无；输出：无。"""
        first = SourceHTTP(
            tmp_path,
            pool_factory=lambda: Pool((b"synthetic",), headers=[(b"etag", b'"synthetic"')]),
        )
        _, old = await first.get(URL)
        old["fetched_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        (first.root / (old["id"] + ".json")).write_bytes(canonical_bytes(old))
        pool = Pool(status=304)
        client = SourceHTTP(
            tmp_path, historical_cache=True, max_total_bytes=2, pool_factory=lambda: pool
        )
        with pytest.raises(DataError, match="EXTERNAL_BYTE_BUDGET_EXCEEDED"):
            await client.get(URL, cache_max_age=21600)
        assert pool.closed and client.records[-1]["status"] == "failed"
        assert not client.revalidations and not client.cache_hits

    asyncio.run(verify())


def test_cache_keeps_original_fetch_and_refresh_gets_new_response(tmp_path):
    """
    作用：缓存不伪装新请求，强制刷新追加原件，原获取时间和旧原件不变。
    输入：合成公开响应。
    输出：无。
    """

    async def run_test():
        """作用：实际走正式抓取/回读接口；输入：无；输出：无。"""
        first = SourceHTTP(tmp_path, pool_factory=lambda: Pool((b"synthetic original",)))
        raw, record = await first.get(URL)
        client = SourceHTTP(
            tmp_path, historical_cache=True, pool_factory=lambda: Pool((b"synthetic new",))
        )
        saved_raw, saved = await client.get(URL, cache_max_age=21600)
        assert saved_raw == raw and saved == record and client.requests == 0
        assert client.cache_hits == {0} and client.bytes_received == len(raw)
        _, fresh = await client.get(URL, cache_max_age=0)
        assert fresh["id"] != record["id"] and client.requests == 1
        assert (client.root / (record["id"] + ".raw")).read_bytes() == raw
        assert json.loads((client.root / (record["id"] + ".json")).read_bytes()) == record

    asyncio.run(run_test())


@pytest.mark.parametrize("change", ["failed", "truncated", "raw", "expired", "future"])
def test_invalid_cache_is_not_used(tmp_path, change):
    """
    作用：失败、截断、篡改或过期/未来原件不能冒充已核验缓存。
    输入：独立合成抓取和损坏方式。
    输出：无。
    """

    async def run_test():
        """作用：破坏缓存后必须真实调用隔离网络；输入：无；输出：无。"""
        source = SourceHTTP(tmp_path, pool_factory=lambda: Pool((b"synthetic original",)))
        _, record = await source.get(URL)
        old = record["id"]
        if change == "failed":
            record["status"] = "failed"
        elif change == "truncated":
            record["truncated"] = True
        elif change == "raw":
            (source.root / (old + ".raw")).write_bytes(b"tampered synthetic")
        else:
            record["fetched_at"] = (
                datetime.now(UTC) + timedelta(hours=1 if change == "future" else -7)
            ).isoformat()
        (source.root / (old + ".json")).write_bytes(canonical_bytes(record))
        client = SourceHTTP(
            tmp_path, historical_cache=True, pool_factory=lambda: Pool((b"new synthetic response",))
        )
        raw, fresh = await client.get(URL, cache_max_age=21600)
        assert raw == b"new synthetic response" and fresh["id"] != old
        assert client.requests == 1 and not client.cache_hits

    asyncio.run(run_test())


def test_cache_is_bounded_and_does_not_apply_to_recent_awc(tmp_path):
    """
    作用：缓存仍受字节/时间额度约束，近期 AWC 不套用历史缓存。
    输入：两种允许来源的合成字节。
    输出：无。
    """

    async def run_test():
        """作用：核对预算和来源限制；输入：无；输出：无。"""
        first = SourceHTTP(tmp_path, pool_factory=lambda: Pool((b"0123456789",)))
        await first.get(URL)
        small = SourceHTTP(tmp_path, historical_cache=True, max_total_bytes=5)
        with pytest.raises(DataError, match="EXTERNAL_BYTE_BUDGET_EXCEEDED"):
            await small.get(URL, cache_max_age=21600)
        assert small.requests == 0
        expired = SourceHTTP(tmp_path, historical_cache=True, timeout=-1)
        with pytest.raises(DataError, match="EXTERNAL_TIMEOUT"):
            await expired.get(URL, cache_max_age=21600)
        awc = SourceHTTP(tmp_path, historical_cache=True, pool_factory=lambda: Pool((b"[]",)))
        await awc.get(AWC_URL, cache_max_age=21600)
        await awc.get(AWC_URL, cache_max_age=21600)
        assert awc.requests == 2 and not awc.cache_hits

    asyncio.run(run_test())
