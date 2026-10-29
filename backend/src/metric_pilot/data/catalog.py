"""只根据服务端登记的 ID 定位不可变快照。"""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from metric_pilot.data.common import DataError, new_id
from metric_pilot.data.contracts import SchemaDTO, SnapshotDTO, SourceDTO
from metric_pilot.data.models import DataSnapshot, DataSource


class Catalog:
    """
    作用：组合 SQLite 仓储和受约束的快照文件目录
    输入：元数据引擎与存储根
    输出：快照目录服务
    """

    def __init__(self, engine, storage_dir: Path):
        """
        作用：固定当前工作区
        输入：engine - 引擎；storage_dir - 存储根
        输出：无
        """
        self.engine = engine
        self.root = storage_dir.resolve()

    def sources(self) -> list[SourceDTO]:
        """
        作用：读取全部实际登记来源
        输入：无
        输出：数据源 DTO 列表
        """
        with Session(self.engine) as session:
            return [
                SourceDTO(
                    id=r.id,
                    name=r.name,
                    source_kind=r.source_kind,
                    current_snapshot_id=r.current_snapshot_id,
                )
                for r in session.scalars(select(DataSource).order_by(DataSource.id))
            ]

    def snapshots(self, source_id: str) -> list[SnapshotDTO]:
        """
        作用：读取来源的全部固定版本
        输入：source_id - 来源标识
        输出：快照 DTO 列表
        """
        with Session(self.engine) as session:
            if session.get(DataSource, source_id) is None:
                raise DataError("SOURCE_NOT_FOUND")
            records = session.scalars(
                select(DataSnapshot)
                .where(DataSnapshot.source_id == source_id)
                .order_by(DataSnapshot.created_at.desc(), DataSnapshot.id)
            )
            return [self._dto(record) for record in records]

    @staticmethod
    def _dto(record) -> SnapshotDTO:
        """
        作用：从持久化记录产生路径无关 DTO
        输入：record - 快照记录
        输出：快照 DTO
        """
        return SnapshotDTO(**{key: getattr(record, key) for key in SnapshotDTO.model_fields})

    def snapshot(self, snapshot_id: str) -> SnapshotDTO:
        """
        作用：取得已登记快照
        输入：snapshot_id - 标识
        输出：快照 DTO
        """
        if not re.fullmatch(r"snapshot_[0-9a-f]{32}", snapshot_id):
            raise DataError("SNAPSHOT_NOT_FOUND")
        with Session(self.engine) as session:
            record = session.get(DataSnapshot, snapshot_id)
            if record is None:
                raise DataError("SNAPSHOT_NOT_FOUND")
            return self._dto(record)

    def database_path(self, snapshot_id: str) -> Path:
        """
        作用：仅将已登记标识映射到受约束路径
        输入：snapshot_id - 标识
        输出：已验证数据库路径
        """
        self.snapshot(snapshot_id)
        path = (self.root / "datasets" / snapshot_id / "analytics.duckdb").resolve()
        if not path.is_relative_to(self.root / "datasets") or not path.is_file():
            raise DataError("SNAPSHOT_FILE_UNAVAILABLE")
        return path

    def schema(self, source_id: str, snapshot_id: str | None = None) -> SchemaDTO:
        """
        作用：读取固定快照的导入时实际结构
        输入：来源与可选快照标识
        输出：结构 DTO
        """
        with Session(self.engine) as session:
            source = session.get(DataSource, source_id)
            if source is None:
                raise DataError("SOURCE_NOT_FOUND")
            selected = snapshot_id or source.current_snapshot_id
            if not selected:
                raise DataError("NO_ACTIVE_SNAPSHOT")
            record = session.get(DataSnapshot, selected)
            if record is None or record.source_id != source_id:
                raise DataError("SNAPSHOT_NOT_FOUND")
            self.database_path(selected)
            return SchemaDTO.model_validate(record.schema)

    def activate(self, source_id: str, snapshot_id: str) -> SourceDTO:
        """
        作用：显式改变默认版本，不修改历史文件
        输入：来源与快照标识
        输出：更新后的来源 DTO
        """
        self.database_path(snapshot_id)
        with Session(self.engine) as session, session.begin():
            source = session.get(DataSource, source_id)
            snapshot = session.get(DataSnapshot, snapshot_id)
            if source is None:
                raise DataError("SOURCE_NOT_FOUND")
            if snapshot.source_id != source_id:
                raise DataError("SNAPSHOT_NOT_FOUND")
            source.current_snapshot_id = snapshot_id
        return next(item for item in self.sources() if item.id == source_id)

    def register(self, snapshot: dict, schema: dict, source_kind: str) -> SnapshotDTO:
        """
        作用：原子登记已写完的快照，默认不激活
        输入：快照信息、固定结构与来源类型
        输出：快照 DTO
        """
        with Session(self.engine) as session, session.begin():
            source = session.scalar(select(DataSource).where(DataSource.source_kind == source_kind))
            if source is None:
                source = DataSource(
                    id=new_id("source"),
                    source_kind=source_kind,
                    name="BTS Reporting Carrier"
                    + (" · 合成测试" if source_kind == "synthetic" else ""),
                    current_snapshot_id=None,
                )
                session.add(source)
                session.flush()
            record = DataSnapshot(**snapshot, source_id=source.id, schema=schema)
            session.add(record)
            session.flush()
            return self._dto(record)
