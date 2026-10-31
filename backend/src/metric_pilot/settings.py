"""应用配置；只检查本地配置完整性，不请求远程模型。"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    作用：从环境变量和 .env 读取应用配置
    输入：可选配置覆盖值
    输出：经过类型校验的配置对象
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    storage_dir: Path = Path("storage")
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    wiki_reasoning_effort: Literal["", "none", "low", "high", "max"] = "none"
    agent_reasoning_effort: Literal["", "none", "low", "high", "max"] = "none"
    run_context_tokens: int = Field(default=32000, ge=8000)
    run_output_tokens: int = Field(default=3000, ge=512, le=16000)
    run_max_turns: int = Field(default=16, ge=1)
    run_max_tool_calls: int = Field(default=24, ge=1)
    run_timeout_seconds: int = Field(default=180, ge=1)
    sql_timeout_seconds: int = Field(default=10, ge=1)
    sql_max_rows: int = Field(default=1000, ge=1)

    def integration_configured(self, prefix: str) -> bool:
        """
        作用：检查模型配置是否完整并排除示例占位值
        输入：prefix - llm 或 embedding
        输出：bool，配置完整时为真；不代表连接已验证
        """
        # 仅检查配置，不返回密钥或连接地址。
        url = getattr(self, f"{prefix}_base_url")
        key = getattr(self, f"{prefix}_api_key").get_secret_value()
        model = getattr(self, f"{prefix}_model")
        return bool(
            url.startswith(("https://", "http://"))
            and "your-provider.example" not in url
            and key
            and key != "replace-me"
            and model
            and not model.startswith("your-")
        )
