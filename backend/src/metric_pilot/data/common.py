"""数据层的标识、规范 JSON 与安全异常。"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4


class DataError(ValueError):
    """
    作用：携带可公开的稳定错误码，不包含底层路径和 SQL 异常文本
    输入：错误码与安全细节
    输出：数据层异常
    """

    def __init__(self, code: str, details: dict | None = None):
        """
        作用：保存安全诊断
        输入：错误码与可选细节
        输出：无
        """
        self.code = code
        self.details = details or {}
        super().__init__(code)


def new_id(prefix: str) -> str:
    """
    作用：生成服务端标识
    输入：标识前缀
    输出：随机唯一标识
    """
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    """
    作用：产生 UTC 时间戳
    输入：无
    输出：带时区 ISO 时间
    """
    return datetime.now(UTC).isoformat()


def canonical_bytes(value) -> bytes:
    """
    作用：生成禁止非有限数字的规范 JSON
    输入：可 JSON 编码值
    输出：UTF-8 字节
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value) -> str:
    """
    作用：计算规范内容哈希
    输入：可 JSON 编码值
    输出：SHA-256
    """
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
