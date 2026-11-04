"""5.D 正式外部适配器的真实 HTTPS 取消验收；隔离数据库、不调用模型。"""

import argparse
import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager

import httpcore
from alembic import command
from alembic.config import Config
from metric_pilot.agent.contracts import CreateRun, WeatherArgs
from metric_pilot.agent.external_tools import ExternalTools
from metric_pilot.agent.store import RunStore
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.external.http import SourceHTTP
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.settings import Settings
from verify_external_evidence import clone_primary
from verify_weather import TrackedBackend


async def verify(args):
    """
    作用：读到真实响应体后设置显式取消信号，检查适配器回收实际网络流。
    输入：服务端固定 DNS 模式。
    输出：真实取消记录，不把未连接成功计为通过。
    """
    original = Settings()
    root = original.storage_dir.resolve()
    folder = root / "validation" / new_id("stage5d_cancel")
    previous = os.environ.get("STORAGE_DIR")
    os.environ["STORAGE_DIR"] = str(folder)
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("STORAGE_DIR", None)
        else:
            os.environ["STORAGE_DIR"] = previous
    config = Settings(_env_file=None, storage_dir=folder)
    source_engine = create_metadata_engine(original)
    engine = create_metadata_engine(config)
    try:
        source = next(
            s
            for s in Catalog(source_engine, root).sources()
            if s.source_kind == "official_bts" and s.current_snapshot_id
        )
        clone_primary(source_engine, engine, source.current_snapshot_id)
        store = RunStore(engine)
        conv = store.conversation()
        fixed = KnowledgeStore(engine).snapshot()
        budget = dict(
            max_turns=16,
            max_tool_calls=24,
            timeout_seconds=120,
            context_tokens=32000,
            output_tokens=3000,
        )
        run = store.create(
            CreateRun(
                conversation_id=conv["id"],
                snapshot_id=source.current_snapshot_id,
                question="真实外部 HTTPS 取消验收",
            ),
            fixed["id"],
            budget,
        )
        store.claim()
        run = store.read(run["id"])
        query = QueryService(Catalog(engine, folder), QueryExecutor(10, 1000))
        cancel = threading.Event()
        adapter = ExternalTools(
            run,
            RunContext(
                run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 120
            ),
            query,
            config,
            cancel,
        )
        streams, pools = [], []
        state = dict(body_bytes_received=0)

        class CancelPool:
            """
            作用：保持正式网络和 TLS 校验，只在实际响应体到达后模拟用户按停止。
            输入：无。
            输出：实际连接池。
            """

            def __init__(self):
                """作用：建立真实受限连接池；输入：无；输出：无。"""
                self.pool = httpcore.AsyncConnectionPool(
                    network_backend=TrackedBackend(
                        "cloudflare" if args.public_dns else "system", streams
                    ),
                    retries=0,
                    max_connections=1,
                    max_keepalive_connections=0,
                )
                pools.append(self.pool)

            async def __aenter__(self):
                """作用：进入真实池；输入：无；输出：自身。"""
                await self.pool.__aenter__()
                return self

            async def __aexit__(self, *exc):
                """作用：关闭真实池；输入：退出状态；输出：无。"""
                await self.pool.__aexit__(*exc)

            @asynccontextmanager
            async def stream(self, *a, **kw):
                """作用：真实响应体到达时发取消信号；输入：请求；输出：实际字节流。"""
                async with self.pool.stream(*a, **kw) as response:

                    async def content():
                        """作用：观测真实体字节后取消；输入：无；输出：响应体。"""
                        async for chunk in response.aiter_stream():
                            state["body_bytes_received"] += len(chunk)
                            cancel.set()
                            await asyncio.sleep(0.2)
                            yield chunk

                    proxy = httpcore.Response(
                        response.status, headers=response.headers, content=content()
                    )
                    try:
                        yield proxy
                    finally:
                        await proxy.aclose()

        http = SourceHTTP(folder, pool_factory=CancelPool, timeout=90)
        adapter.http = http
        try:
            result = await asyncio.wait_for(
                adapter.invoke(
                    "query_historical_weather",
                    WeatherArgs(airport="ATL", start=args.start, refresh=True),
                ),
                95,
            )
            raise AssertionError(
                f"CANCEL_NOT_PROPAGATED: body_bytes={state['body_bytes_received']}, "
                f"tool_error={result.error_code}, fetches={len(http.records)}"
            )
        except DataError as exc:
            assert exc.code == "RUN_CANCELLED", exc.code
        store.finish(run["id"], "cancelled")
        assert state["body_bytes_received"] > 0
        assert streams and all(s.closed for s in streams)
        assert all(not p.connections for p in pools)
        assert (
            http.records
            and http.records[-1]["status"] == "failed"
            and http.records[-1]["truncated"]
        )
        assert not list((folder / "external/frozen/snapshots").glob("*"))
        record = dict(
            passed=True,
            checked_at=utc_now(),
            model_calls=0,
            requested_start=args.start,
            **state,
            streams_closed=all(s.closed for s in streams),
            pools_empty=True,
            run_status=store.read(run["id"])["status"],
            fetch=http.records[-1],
            scope="正式适配器和真实HTTPS；模型消息配对另在隔离循环测试",
        )
        (root / "validation/stage5d-cancellation.json").write_bytes(canonical_bytes(record))
        print(
            "真实 HTTPS 取消通过：响应体已到达，实际流关闭、连接池清空、运行取消，无成功外部证据。"
        )
    except BaseException:
        # 验收失败也结束隔离运行；原始失败请求保留，不覆盖此前成功记录冒充本轮结果。
        if "store" in locals() and "run" in locals():
            current = store.read(run["id"])
            if current["status"] in {"queued", "running"}:
                store.finish(run["id"], "failed", error_code="VALIDATION_FAILED")
        raise
    finally:
        source_engine.dispose()
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-dns", action="store_true")
    parser.add_argument("--start", default="yesterday")
    asyncio.run(verify(parser.parse_args()))
