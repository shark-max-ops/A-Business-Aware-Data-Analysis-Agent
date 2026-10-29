"""真实 spawn 子进程查询；墙钟超时和取消都回收底层进程。"""

import multiprocessing as mp
import signal
import time
from pathlib import Path

import duckdb

from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.data.serialization import encode_cell

MAX_BYTES = 2 * 1024 * 1024
CONNECTION_CONFIG = {
    "enable_external_access": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "threads": "2",
    "memory_limit": "512MB",
    "max_temp_directory_size": "0B",
}


def open_analysis_connection(path: Path):
    """
    作用：打开与导入连接分离的受限只读连接
    输入：服务端已验证的快照路径
    输出：DuckDB 连接；不支持配置时直接失败
    """
    connection = duckdb.connect(str(path), read_only=True, config=CONNECTION_CONFIG)
    try:
        # 这是连接级设置，不能放入全局连接配置；避免慢查询进度干扰 CLI 输出。
        connection.execute("SET enable_progress_bar = false")
    except Exception:
        connection.close()
        raise
    return connection


def query_child(send, path: str, sql: str, parameters: dict, max_rows: int, max_bytes: int):
    """
    作用：在固定策略下执行 SQL 并输出有限类型化结果
    输入：IPC、已验证路径和 SQL、标量绑定与服务端上限
    输出：IPC 开始通知及成功结果或安全错误码
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    connection = None
    try:
        connection = open_analysis_connection(Path(path))
        send.send(("executing", None))
        cursor = connection.execute(sql, parameters)
        columns = [{"name": item[0], "type": str(item[1])} for item in cursor.description]
        if len({c["name"] for c in columns}) != len(columns):
            raise DataError("DUPLICATE_RESULT_COLUMNS")
        rows, size, truncated = [], len(canonical_bytes({"columns": columns, "rows": []})), False
        if size > max_bytes:
            raise DataError("RESULT_TOO_LARGE")
        for index in range(max_rows + 1):
            raw = cursor.fetchone()
            if raw is None:
                break
            if index == max_rows:
                truncated = True
                break
            row = [encode_cell(value) for value in raw]
            row_size = len(canonical_bytes(row)) + 1
            if size + row_size > max_bytes:
                if not rows:
                    raise DataError("RESULT_TOO_LARGE")
                truncated = True
                break
            rows.append(row)
            size += row_size
        send.send(("result", {"columns": columns, "rows": rows, "truncated": truncated}))
    except DataError as exc:
        send.send(("error", exc.code))
    except duckdb.Error:
        send.send(("error", "SQL_EXECUTION_FAILED"))
    except Exception:
        send.send(("error", "QUERY_WORKER_FAILED"))
    finally:
        if connection:
            connection.close()
        send.close()


class QueryExecutor:
    """
    作用：监督查询子进程并保证返回前完成回收
    输入：服务端行数、时间与字节预算
    输出：可复用执行器
    """

    def __init__(self, timeout_seconds=10, max_rows=1000, max_bytes=MAX_BYTES):
        """
        作用：固定当前执行策略
        输入：超时、最大行数与字节数
        输出：无
        """
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self.max_bytes = min(max_bytes, MAX_BYTES)

    def execute(
        self, path: Path, guarded, parameters: dict, deadline: float, cancel=None, on_started=None
    ) -> dict:
        """
        作用：执行受检查询并响应显式取消及墙钟截止时间
        输入：快照、受检计划、参数、截止时间、取消信号和可选开始通知
        输出：冻结结果；超时或取消前终止实际子进程
        """
        context = mp.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=query_child,
            args=(send, str(path), guarded.limited_sql, parameters, self.max_rows, self.max_bytes),
            daemon=True,
        )
        stop_at = min(deadline, time.monotonic() + self.timeout_seconds)
        started = False
        try:
            if cancel is not None and cancel.is_set():
                raise DataError("QUERY_CANCELLED")
            if time.monotonic() >= stop_at:
                raise DataError("QUERY_TIMEOUT")
            process.start()
            started = True
            send.close()
            while True:
                if cancel is not None and cancel.is_set():
                    raise DataError("QUERY_CANCELLED")
                if time.monotonic() >= stop_at:
                    raise DataError("QUERY_TIMEOUT")
                if receive.poll(min(0.02, max(0, stop_at - time.monotonic()))):
                    try:
                        kind, value = receive.recv()
                    except (EOFError, OSError):
                        raise DataError("QUERY_WORKER_FAILED") from None
                    if kind == "executing":
                        if on_started:
                            on_started(process.pid)
                    elif kind == "error":
                        raise DataError(value)
                    elif kind == "result":
                        if time.monotonic() >= stop_at:
                            raise DataError("QUERY_TIMEOUT")
                        if cancel is not None and cancel.is_set():
                            raise DataError("QUERY_CANCELLED")
                        return value
                elif not process.is_alive():
                    raise DataError("QUERY_WORKER_FAILED")
        finally:
            # 不使用线程超时替代查询终止；所有路径都 join，必要时 terminate/kill。
            if started:
                process.join(timeout=0.1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                if process.is_alive():
                    raise RuntimeError("QUERY_PROCESS_NOT_REAPED")
                process.close()
            receive.close()
            send.close()
