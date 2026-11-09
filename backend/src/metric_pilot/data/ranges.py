"""服务端日期范围：左闭右开，范围与固定数据快照共同构成证据边界。"""

from datetime import date

from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from metric_pilot.contracts import StrictDTO
from metric_pilot.data.common import DataError
from metric_pilot.data.models import DataSnapshot


class DateRange(StrictDTO):
    """
    作用：严格表达包含开始、不包含结束的业务日期范围
    输入：ISO日期字符串
    输出：非空且顺序正确的日期范围
    """

    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_exclusive: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def valid_dates(self):
        """
        作用：检查真实日历与区间顺序，拒绝日期格式注入
        输入：当前对象
        输出：当前对象或校验错误
        """
        if date.fromisoformat(self.start) >= date.fromisoformat(self.end_exclusive):
            raise ValueError("INVALID_DATE_RANGE")
        return self


def snapshot_range(snapshot, requested=None, *, require_explicit=False):
    """
    作用：从可信快照确定覆盖并验证请求，不使用模型声明扩大范围
    输入：快照记录/DTO、可选范围及多月显式选择要求
    输出：规范范围
    """
    if snapshot.period:
        first = date.fromisoformat(snapshot.period + "-01")
        end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        coverage = DateRange(start=first.isoformat(), end_exclusive=end.isoformat())
        periods = [snapshot.period]
    else:
        if requested is None and require_explicit:
            raise DataError("DATE_RANGE_REQUIRED")
        coverage = DateRange(
            start=snapshot.manifest["coverage_start"],
            end_exclusive=snapshot.manifest["coverage_end_exclusive"],
        )
        periods = snapshot.manifest["periods"]
    selected = coverage if requested is None else DateRange.model_validate(requested)
    if selected.start < coverage.start or selected.end_exclusive > coverage.end_exclusive:
        raise DataError("DATE_RANGE_OUTSIDE_SNAPSHOT")
    cursor = date.fromisoformat(selected.start).replace(day=1)
    while cursor.isoformat() < selected.end_exclusive:
        if cursor.isoformat()[:7] not in periods:
            raise DataError("DATE_RANGE_MISSING_MONTH")
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return selected


def resolve_range(engine, snapshot_id, requested=None, *, require_explicit=False):
    """
    作用：只从服务端已登记快照校验范围
    输入：元数据库引擎、快照ID、请求范围和显式要求
    输出：规范范围
    """
    with Session(engine) as session:
        snapshot = session.get(DataSnapshot, snapshot_id)
        if snapshot is None:
            raise DataError("SNAPSHOT_NOT_FOUND")
        return snapshot_range(snapshot, requested, require_explicit=require_explicit)


def run_range(engine, run):
    """
    作用：恢复冻结运行范围，旧单月记录缺字段时按原月份兼容
    输入：引擎与可信运行/调查字典
    输出：规范范围，不修改旧记录
    """
    return resolve_range(engine, run["snapshot_id"], run.get("date_range"))


def scope_sql(selected):
    """
    作用：生成服务端专用临时视图约束，模型仍无DDL或限定库表权限
    输入：已验证的DateRange
    输出：在只读查询子进程内执行的固定SQL
    """
    selected = DateRange.model_validate(selected)
    return (
        'CREATE TEMP VIEW "v_flights" AS SELECT * FROM "analytics"."main"."v_flights" '
        f"WHERE flight_date >= DATE '{selected.start}' "
        f"AND flight_date < DATE '{selected.end_exclusive}'"
    )
