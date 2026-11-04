"""阶段 5.A 独立入口；不启动后台 Agent 或发出模型请求。"""

import argparse
import asyncio
import json
import sys
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from metric_pilot.data.common import DataError
from metric_pilot.external.http import SourceHTTP
from metric_pilot.external.registry import SOURCE_POLICIES, STATIONS
from metric_pilot.external.weather import WeatherRequest, query_historical_weather
from metric_pilot.settings import Settings

MESSAGES = {
    "WEATHER_STATION_NOT_FOUND": "机场尚未登记；当前支持 ATL、ORD、JFK。",
    "WEATHER_STATION_AMBIGUOUS": "存在多个适用站点，需要先核对映射。",
    "WEATHER_MAPPING_DATE": "该日期超出已核对的站点映射范围（2026 年）。",
    "WEATHER_DATE_RANGE": "仅查询已结束的当地日期，单次最多 3 天。",
    "WEATHER_DATE_INVALID": "日期不存在，请使用 YYYY-MM-DD 或 yesterday。",
    "WEATHER_FIELD_INVALID": "天气字段无效或重复，请核对使用说明。",
    "EXTERNAL_ADDRESS_REJECTED": (
        "目的地址不是允许的公网地址；虚拟 DNS 环境可显式选择 --public-dns。"
    ),
    "EXTERNAL_CANCELLED": "已取消实际天气请求。",
    "EXTERNAL_TIMEOUT": "来源请求超时，已关闭读取；没有成功结果。",
    "EXTERNAL_CONNECTION_FAILED": "天气来源连接失败；没有自动重试。",
    "EXTERNAL_HTTP_ERROR": "天气来源拒绝或无法处理请求；请查看抓取清单中的 HTTP 状态。",
    "EXTERNAL_RESPONSE_LIMIT": "来源响应超过读取上限，已关闭连接，不使用截断结果。",
    "WEATHER_PROVIDER_RESULT_LIMIT": "达到来源返回条数上限，不能当成完整天气记录。",
    "WEATHER_STATION_MISMATCH": "原始观测的站点标识或位置与登记映射不符，已停止。",
    "WEATHER_SOURCE_FORMAT": "来源格式不符合约定，未生成成功查询。",
}

FIELD_LABELS = {
    "temperature": ("温度", "°C"),
    "wind_speed": ("风速", "米/秒"),
    "visibility": ("能见度", "公里"),
    "precipitation": ("降水", "毫米"),
    "snow_depth": ("积雪深度", "毫米"),
}


def display_number(value):
    """
    作用：将已取得的观测值格式化为最多两位小数，不改保存的精度。
    输入：数值。
    输出：短数字文本。
    """
    return f"{value:.2f}".rstrip("0").rstrip(".")


def text_table(headers, rows):
    """
    作用：按中文字符显示宽度排列纯文本表格。
    输入：表头和字符串行。
    输出：表格文本。
    """

    def width(value):
        """
        作用：计算中英文混排的终端列宽。
        输入：文本。
        输出：显示列数。
        """
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in value)

    widths = [max(width(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    lines = []
    for row in [headers, *rows]:
        lines.append(
            " | ".join(
                value + " " * (size - width(value)) for value, size in zip(row, widths, strict=True)
            ).rstrip()
        )
        if len(lines) == 1:
            lines.append("-+-".join("-" * size for size in widths))
    return "\n".join(lines)


def readable_weather(result, storage, row_limit):
    """
    作用：将真实天气结果展示为中文摘要、当地时间明细及来源位置。
    输入：已保存结果、存储目录和明细显示上限。
    输出：终端可读文本；不计算新天气统计或修改结果。
    """
    mapping = result["mapping"]
    observations = result["observations"]
    fields = [field for field in FIELD_LABELS if field in result["coverage"]]
    period = result["local_start"]
    if result["local_end"] != period:
        period += " 至 " + result["local_end"]
    source = (
        "NOAA AWC（近期观测）" if result["source"] == "awc_metar" else "NOAA NCEI GHCNh（历史观测）"
    )
    lines = [
        f"{'查询成功' if observations else '查询完成，未取得观测'}："
        f"{mapping['airport']}，{period}（机场当地日期）",
        f"时区：{mapping['timezone']}；来源：{source}",
        f"取得 {result['observation_count']} 条观测；站点 {mapping['icao']}，"
        f"距机场参考点约 {mapping['distance_km']} 公里。",
        "",
    ]
    # 直接展示服务端已有覆盖和极值，不把记录频次当作连续覆盖或平均值。
    for field in fields:
        coverage = result["coverage"][field]
        label, unit = FIELD_LABELS[field]
        available = coverage["hours_with_observation"]
        expected = coverage["expected_utc_hours"]
        low, high = coverage["exact_min"], coverage["exact_max"]
        if low is not None and high is not None:
            detail = f"精确观测值 {display_number(low)}～{display_number(high)} {unit}"
        elif field in ("precipitation", "snow_depth"):
            detail = "只展示原值，不累计" if available else "无可用观测，不当作零"
        else:
            detail = "无可用精确值，界限值见明细" if available else "无可用观测，不当作零"
        lines.append(f"{label}：{detail}；{available}/{expected} 个小时有可用观测。")
    lines.append("注：有观测表示该小时至少一条符合筛选的记录，不代表连续完整覆盖。")

    # 冲突及未通过筛选的原值仍可查看，但显式标记，避免展示成可靠观测。
    signatures = {}
    for observation in observations:
        for field, cell in observation["fields"].items():
            if cell["accepted"]:
                signatures.setdefault((observation["observed_at"], field), set()).add(
                    (cell["value"], cell["qualifier"])
                )
    rows = []
    zone = ZoneInfo(mapping["timezone"])
    for observation in observations[:row_limit]:
        local = datetime.fromisoformat(observation["observed_at"]).astimezone(zone)
        row = [local.strftime("%m-%d %H:%M %z")]
        for field in fields:
            cell = observation["fields"][field]
            if cell["value"] is None:
                row.append("缺测/无法解析")
                continue
            value = cell["qualifier"] + display_number(cell["value"])
            if (
                not cell["accepted"]
                or len(signatures.get((observation["observed_at"], field), ())) > 1
            ):
                value += "*"
            row.append(value)
        rows.append(row)
    if rows:
        headers = [
            "当地时间（含UTC偏移）",
            *[f"{FIELD_LABELS[field][0]}（{FIELD_LABELS[field][1]}）" for field in fields],
        ]
        lines.extend(["", "观测明细（显示值最多保留两位小数）", text_table(headers, rows)])
        lines.append("* 表示未通过筛选或同刻有冲突，不纳入摘要；> 表示大于该值。")
        if len(observations) > row_limit:
            lines.append(
                f"当前显示前 {len(rows)}/{len(observations)} 条；"
                f"加 --rows {len(observations)} 可显示全部。"
            )
    else:
        lines.append("没有取得该日期范围的观测，不能将缺失解释成零。")
    if any(field in fields for field in ("precipitation", "snow_depth")):
        lines.append("降水可能含累计/微量等测量标记；本工具不计算总降水，详见保存原件。")
    lines.extend(
        [
            "",
            "出处与完整记录：",
            f"数据接口：{result['fetches'][0]['url']}" if result["fetches"] else "无抓取来源",
            f"完整结果：{storage / 'external' / 'weather' / (result['id'] + '.json')}",
            f"原件与请求条件：{storage / 'external' / 'fetches'}（完整结果中可查对应文件）",
            "以上为独立天气查询，尚未接入分析对话和报告；天气观测不能单独证明航班异常原因。",
        ]
    )
    return "\n".join(lines)


def main():
    """
    作用：独立查询天气并输出中文摘要和保存标识，Ctrl+C 取消实际请求。
    输入：命令行机场、日期和字段；仅从配置取存储目录。
    输出：默认中文摘要与明细，--json 输出技术记录；错误非零退出。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立天气查询（尚未接入分析 Agent）")
    parser.add_argument("--list-airports", action="store_true")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON，供技术核对")
    parser.add_argument("--rows", type=int, default=20, help="明细显示条数，默认 20，最多 20000")
    parser.add_argument(
        "--public-dns",
        action="store_true",
        help="显式使用 1.1.1.1 加密 DNS，仅发送 NOAA 域名；用于本机虚拟 DNS",
    )
    parser.add_argument("--airport")
    parser.add_argument("--start", help="机场当地日期 YYYY-MM-DD 或 yesterday")
    parser.add_argument("--end", help="机场当地结束日期，含当天，最多 3 天")
    parser.add_argument("--fields", nargs="+", default=["temperature", "wind_speed", "visibility"])
    parser.add_argument(
        "--refresh", action="store_true", help="明确要求重新获取；独立CLI默认已不使用历史缓存"
    )
    args = parser.parse_args()
    if not 1 <= args.rows <= 20000:
        parser.error("--rows 必须在 1～20000 之间")
    try:
        if args.list_airports:
            output = dict(airports=[s.detail() for s in STATIONS], sources=SOURCE_POLICIES)
            if not args.json:
                print("当前支持的机场（映射仅适用 2026 年）：")
                print(
                    text_table(
                        ["机场", "观测站", "当地时区"],
                        [[station.airport, station.icao, station.timezone] for station in STATIONS],
                    )
                )
                return
        else:
            request = WeatherRequest(
                airport=(args.airport or "").upper(),
                start=args.start or "",
                end=args.end,
                fields=args.fields,
                refresh=args.refresh,
            )
            storage = Settings().storage_dir
            http = SourceHTTP(storage, dns_mode="cloudflare" if args.public_dns else "system")
            output = asyncio.run(query_historical_weather(request, storage, http=http))
            if not args.json:
                print(readable_weather(output, storage, args.rows))
                return
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    except (DataError, ValidationError) as error:
        code = error.code if isinstance(error, DataError) else "INVALID_ARGUMENT"
        if not args.json:
            print(f"查询未完成：{MESSAGES.get(code, '请核对参数及来源状态。')} [{code}]")
            raise SystemExit(1) from None
        print(
            json.dumps(
                {
                    "error": {
                        "code": code,
                        "message": MESSAGES.get(code, "天气查询未完成，请核对参数及来源状态。"),
                    }
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(
            '{"error":{"code":"EXTERNAL_CANCELLED","message":"已取消实际天气请求"}}'
            if args.json
            else "已取消实际天气请求。"
        )
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
