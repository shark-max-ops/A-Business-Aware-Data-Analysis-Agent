"""Server-owned date labels; legacy projection never mutates frozen records."""

from datetime import date
from typing import Literal

from metric_pilot.contracts import StrictDTO
from metric_pilot.data.ranges import DateRange


class ScopeContext(StrictDTO):
    policy: Literal["server_scope_v1", "legacy_scope_v1"]
    main_date_range: DateRange | None
    range_source: Literal["frozen_range", "legacy_single_month", "unavailable"]
    main_label: str


def scope_context(payload, *, new=False):
    if not new and payload.get("scope_context"):
        return ScopeContext.model_validate(payload["scope_context"]).model_dump()
    selected = payload.get("date_range")
    source = "frozen_range" if selected else "unavailable"
    if not selected:
        period = payload.get("data_snapshot", {}).get("period")
        if period:
            first = date.fromisoformat(period + "-01")
            end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
            selected = {"start": first.isoformat(), "end_exclusive": end.isoformat()}
            source = "legacy_single_month"
    if selected:
        selected = DateRange.model_validate(selected).model_dump()
        label = f"调查主范围：{selected['start']}（含）至{selected['end_exclusive']}（不含）"
        if source == "legacy_single_month":
            label += "（按历史单月快照恢复）"
    else:
        label = "历史范围未记录"
    return {
        "policy": "server_scope_v1" if new else "legacy_scope_v1",
        "main_date_range": selected,
        "range_source": source,
        "main_label": label,
    }
