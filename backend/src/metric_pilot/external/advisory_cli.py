"""5.B 独立公告终端：中文候选与带行号的官方原文，不调用模型。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

from pydantic import ValidationError

from metric_pilot.data.common import DataError
from metric_pilot.external.advisories import (
    AdvisorySearch,
    page_search,
    read_external_source,
    search_official_advisories,
)
from metric_pilot.external.advisory_parser import ACTIONS, KINDS
from metric_pilot.external.cli import MESSAGES
from metric_pilot.external.http import SourceHTTP
from metric_pilot.settings import Settings

ERRORS = {
    "ADVISORY_DATE_RANGE": "UTC 签发日期必须在 2000 年至今天之间，单次最多三天，类型不能重复。",
    "ADVISORY_SOURCE_REF_INVALID": "来源编号格式不正确；请使用查询返回的编号，不要输入网址。",
    "ADVISORY_SOURCE_NOT_FOUND": "未找到已登记来源，请先查询候选清单。",
    "ADVISORY_SOURCE_FORMAT": "官方页面未返回预期清单或正文，未当作空结果或使用摘要替代。",
    "ADVISORY_LINK_REJECTED": "清单包含越界或无效正文链接，已拒绝访问。",
    "ADVISORY_IDENTITY_MISMATCH": "公告编号、日期、标题或签发时间与清单不一致，已停止。",
    "ADVISORY_SCOPE_MISMATCH": "正文的控制设施与候选不一致，已停止。",
    "ADVISORY_FILE_CHANGED": "固定资料或原件校验失败，不能继续使用。",
    "ADVISORY_SOURCE_INVALID": "来源与已保存清单不一致，不能读取。",
    "ADVISORY_PAGE_INVALID": "行偏移不能为负，单页最多 100 行。",
}


def readable_search(result):
    """
    作用：显示候选清单和可复制的原文读取编号。
    输入：实际候选页。
    输出：中文终端文本。
    """
    request = result["request"]
    fetch = result["fetches"][0]
    source_url = fetch["url"] + "?" + urlencode(fetch["params"])
    lines = [
        f"公告查询完成：{request['airport']}，{request['start']}"
        f" 至 {request['end'] or request['start']}（UTC 签发日）",
        f"匹配 {result['total']} 条候选；本页 {len(result['items'])} 条。",
        "部分日期未完成："
        + "、".join(d["utc_date"] for d in result["unavailable_dates"])
        + "（中途失败后停止；技术原因可用 --json 查看）"
        if result.get("partial")
        else "请求日期清单均已核对。",
        "候选尚未核实正文；无匹配不代表没有事件。",
        "",
    ]
    for index, item in enumerate(result["items"], result["offset"] + 1):
        lines.extend(
            [
                f"{index}. #{item['number']:03d} | {item['issued_at'][:16].replace('T', ' ')} UTC"
                f" | {item['facility']}",
                f"   {KINDS[item['kind']]}；{ACTIONS[item['action']]}",
                f"   原标题：{item['title']}",
                f"   正文编号：{item['source_ref']}",
            ]
        )
    lines.extend(
        [
            "",
            f"固定清单编号：{result['id']}",
            "读取原文：python -m metric_pilot.external.advisory_cli read <正文编号> --public-dns",
        ]
    )
    if result["next_offset"] is not None:
        lines.append(f"下一页：list {result['id']} --offset {result['next_offset']}")
    lines.extend(
        [
            "签发日、有效期和机场当地日期不同；本工具不直接判断航班异常原因。",
            f"官方清单：{source_url}",
        ]
    )
    return "\n".join(lines)


def readable_body(result):
    """
    作用：按行展示固定官方原文，中文说明范围和下一页，保持英文原文不改写。
    输入：实际正文页。
    输出：终端文本。
    """
    candidate = result["candidate"]
    source_url = result["fetch"]["url"] + "?" + urlencode(result["fetch"]["params"])
    lines = [
        f"公告原文：#{candidate['number']:03d} {candidate['facility']} {candidate['title']}",
        f"类型：{KINDS[candidate['kind']]}；{ACTIONS[candidate['action']]}",
        f"签发：{result['signed_at']}（UTC）",
        f"原始有效时间：{result['effective_time_raw']}（与签发时间不同）",
    ]
    if result["effective_window"]:
        window = result["effective_window"]
        lines.append(f"解析有效期：{window['start_utc']} 至 {window['end_utc']}")
    else:
        lines.append("有效期未能可靠解析，请核对原文，未猜测起止。")
    scope = result["carrier_scope"]
    lines.append(
        "航司范围：发现限制性语句，不能推广为全部航司。"
        if scope["clauses"]
        else "航司范围：未自动确定，请按原文核对。"
    )
    lines.extend(["", "以下为官方原文（英文保留，不由模型翻译或改写）："])
    lines.extend(f"{line['line']:>4} | {line['text']}" for line in result["lines"])
    lines.extend(
        [
            "",
            f"显示 {len(result['lines'])} 行 / 共 {result['total_lines']} 行。",
            f"固定正文编号：{result['body_ref']}",
            f"官方原文：{source_url}",
        ]
    )
    if result["next_offset"] is not None:
        lines.append(
            f"下一页（不重新联网）：read {result['body_ref']} --offset {result['next_offset']}"
        )
    lines.append("管制取消不等于航班取消；预计影响不等于 BTS 实际统计；引用须经过固定证据授权。")
    return "\n".join(lines)


def main():
    """
    作用：提供查候选、固定清单翻页和正文读取三个独立终端操作。
    输入：命令行参数。
    输出：中文结果；--json 输出技术字段，失败非零退出。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立 FAA ATCSCC 公告查询，非完整 NOTAM 服务")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="按 FAA 设施首段和 UTC 签发日查候选")
    search.add_argument("--airport", required=True, help="三字母设施/机场代码，例如 DFW")
    search.add_argument("--start", required=True, help="UTC 签发日期 YYYY-MM-DD")
    search.add_argument("--end")
    search.add_argument("--types", nargs="+", choices=list(KINDS), default=[])
    listing = sub.add_parser("list", help="回读固定候选清单，不联网")
    listing.add_argument("search_id")
    reading = sub.add_parser("read", help="读登记候选原文，或固定正文下一页")
    reading.add_argument("source_ref")
    for command in (search, listing, reading):
        command.add_argument("--json", action="store_true")
        command.add_argument(
            "--public-dns", action="store_true", help="显式向 1.1.1.1 仅发送 FAA 域名以解析公网 IP"
        )
    for command in (listing, reading):
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--limit", type=int, default=40 if command is reading else 20)
    args = parser.parse_args()
    storage = Path(Settings().storage_dir)
    try:
        http = SourceHTTP(
            storage,
            max_requests=3 if args.command == "search" else 1,
            dns_mode="cloudflare" if args.public_dns else "system",
        )
        if args.command == "search":
            request = AdvisorySearch(
                airport=args.airport.upper(), start=args.start, end=args.end, types=args.types
            )
            result = asyncio.run(search_official_advisories(request, storage, http=http))
        elif args.command == "list":
            result = page_search(args.search_id, storage, args.offset, args.limit)
        else:
            result = asyncio.run(
                read_external_source(
                    args.source_ref, storage, offset=args.offset, limit=args.limit, http=http
                )
            )
        print(
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.json
            else (readable_body(result) if args.command == "read" else readable_search(result))
        )
    except (DataError, ValidationError) as error:
        code = error.code if isinstance(error, DataError) else "INVALID_ARGUMENT"
        message = ERRORS.get(code, MESSAGES.get(code, "公告查询未完成，请核对参数及来源状态。"))
        print(
            json.dumps(dict(error=dict(code=code, message=message)), ensure_ascii=False)
            if args.json
            else f"公告操作未完成：{message} [{code}]"
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("已取消实际公告请求，未保存成功正文。")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
