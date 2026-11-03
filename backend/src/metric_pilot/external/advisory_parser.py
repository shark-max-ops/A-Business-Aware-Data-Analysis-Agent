"""FAA 公开 HTML 的受限解析；清单是候选，原始正文与项目标签分开。"""

import calendar
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from metric_pilot.data.common import DataError
from metric_pilot.external.registry import FAA_ROOT

KINDS = {
    "ground_stop": "地面停止",
    "ground_delay": "地面延误计划",
    "arrival_delay": "到达延误通知",
    "operations_plan": "运行安排",
    "other": "其他通知",
}
ACTIONS = {
    "proposed": "拟议通知，不能视为已经实施",
    "cancelled": "管制取消通知（不是航班取消）",
    "extended": "延长通知",
    "updated": "更新通知",
    "unspecified": "签发通知，变更状态未明确",
}


class AdvisoryHTML(HTMLParser):
    """
    作用：收集表格单元格、链接和 PRE 原文，不执行网页脚本。
    输入：FAA HTML。
    输出：带 HTML 行号的表格行。
    """

    def __init__(self):
        """
        作用：初始化有限解析状态。
        输入：无。
        输出：无。
        """
        super().__init__(convert_charrefs=True)
        self.stack, self.rows = [], []
        self.hidden = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        """
        作用：记录表格、链接和原文块，并忽略脚本/样式。
        输入：标签与属性。
        输出：无。
        """
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag == "tr":
            if len(self.stack) >= 32:
                raise DataError("ADVISORY_SOURCE_FORMAT")
            self.stack.append(dict(cells=[], cell=None, html_line=self.getpos()[0]))
        if self.stack and tag in ("td", "th"):
            cell = dict(text=[], pre=[], has_pre=False, links=[], tag=tag)
            self.stack[-1]["cells"].append(cell)
            self.stack[-1]["cell"] = cell
        cell = self.stack[-1]["cell"] if self.stack else None
        if cell is not None:
            if tag == "a":
                cell["links"].append(dict(attrs).get("href", ""))
            if tag == "pre":
                self.pre += 1
                cell.update(has_pre=True, pre_html_line=self.getpos()[0])
            if tag == "br":
                cell["text"].append("\n")
                if self.pre:
                    cell["pre"].append("\n")

    def handle_endtag(self, tag):
        """
        作用：结束块并保存完整表格行。
        输入：结束标签。
        输出：无。
        """
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in ("td", "th") and self.stack:
            self.stack[-1]["cell"] = None
        if tag == "tr" and self.stack:
            if len(self.rows) >= 10000:
                raise DataError("ADVISORY_RESULT_LIMIT")
            row = self.stack.pop()
            self.rows.append(row)

    def handle_data(self, data):
        """
        作用：保存可见文字，原文块保留换行。
        输入：文本。
        输出：无。
        """
        if not self.hidden and self.stack and self.stack[-1]["cell"] is not None:
            cell = self.stack[-1]["cell"]
            cell["text"].append(data)
            if self.pre:
                cell["pre"].append(data)


def compact(cell):
    """
    作用：合并标签内排版空白，不用于正文 PRE。
    输入：单元格。
    输出：短文本。
    """
    return " ".join("".join(cell["text"]).split())


def parse_html(raw):
    """
    作用：按该入口实际 ISO-8859-1 HTML 解析，拒绝空白和不完整表格结构。
    输入：有界原始字节。
    输出：表格结构。
    """
    parser = AdvisoryHTML()
    parser.feed(raw.decode("iso-8859-1"))
    parser.close()
    if not parser.rows or parser.pre:
        raise DataError("ADVISORY_SOURCE_FORMAT")
    return parser.rows


def classify(title):
    """
    作用：按公告标题给保守项目标签，不从正文其他计划的取消推断本计划状态。
    输入：官方原标题。
    输出：公告类型与动作。
    """
    upper = title.upper()
    if re.search(r"\bGROUND STOP\b|\bGS\b", upper):
        kind = "ground_stop"
    elif re.search(r"\bGROUND DELAY\b|\bGDP\b", upper):
        kind = "ground_delay"
    elif "ARRIVAL DELAY" in upper:
        kind = "arrival_delay"
    elif "OPERATIONS PLAN" in upper:
        kind = "operations_plan"
    else:
        kind = "other"
    if re.search(r"\bPROPOSED\b", upper):
        action = "proposed"
    elif re.search(r"\bCNX\b|\bCANCEL(?:LED|ED|LATION)?\b", upper):
        action = "cancelled"
    elif re.search(r"\bEXTEND\w*|\bEXTENSION\b", upper):
        action = "extended"
    elif re.search(r"\bUPDATE\w*|\bREVISION\b|\bAMEND\w*", upper):
        action = "updated"
    else:
        action = "unspecified"
    return kind, action


def registered_link(href, day):
    """
    作用：仅接受实际清单中同日、同站的正文链接。
    输入：原始链接与请求 UTC 日期。
    输出：已校验日期、编号；危险链接立即拒绝。
    """
    try:
        part = urlsplit(urljoin(FAA_ROOT + "/adv_list", href))
    except ValueError:
        raise DataError("ADVISORY_LINK_REJECTED") from None
    query = parse_qs(part.query, keep_blank_values=True)
    if (
        part.scheme != "https"
        or part.netloc != "www.fly.faa.gov"
        or part.path != "/adv/adv_otherdis"
        or part.fragment
        or set(query) != {"adv_date", "advn"}
        or any(len(value) != 1 for value in query.values())
    ):
        raise DataError("ADVISORY_LINK_REJECTED")
    if query["adv_date"][0] != day.strftime("%m%d%Y"):
        raise DataError("ADVISORY_IDENTITY_MISMATCH")
    number = query["advn"][0]
    if not re.fullmatch(r"\d{1,4}", number) or int(number) < 1:
        raise DataError("ADVISORY_LINK_REJECTED")
    return int(number)


def parse_list(raw, day):
    """
    作用：核对清单日期、列结构及每行身份，不把 HTTP200 错误页当空结果。
    输入：原件和请求 UTC 日期。
    输出：候选元数据，尚未核实正文。
    """
    rows = parse_html(raw)
    titles = [compact(c) for row in rows for c in row["cells"] if c["tag"] == "th"]
    expected = "ATCSCC ADVISORIES FOR " + day.isoformat()
    if expected not in titles or not {
        "NUMBER",
        "CONTROL ELEMENT",
        "DATE",
        "BRIEF TITLE",
        "SEND TIME",
    }.issubset(titles):
        raise DataError("ADVISORY_SOURCE_FORMAT")
    results, seen = [], set()
    for row in rows:
        cells = row["cells"]
        links = [link for c in cells for link in c["links"] if "adv_otherdis" in link]
        if not links:
            if len(cells) == 5 and re.fullmatch(r"\d+", compact(cells[0])):
                raise DataError("ADVISORY_SOURCE_FORMAT")
            continue
        if len(cells) != 5 or len(links) != 1:
            raise DataError("ADVISORY_SOURCE_FORMAT")
        number = registered_link(links[0], day)
        values = [compact(c) for c in cells]
        try:
            issued = datetime.strptime(values[4], "%m/%d/%y %H:%M").replace(tzinfo=UTC)
            identity = (int(values[0]), datetime.strptime(values[2], "%m/%d/%y").date())
        except ValueError:
            raise DataError("ADVISORY_SOURCE_FORMAT") from None
        if identity != (number, day) or issued.date() != day or number in seen:
            raise DataError("ADVISORY_IDENTITY_MISMATCH")
        if not re.fullmatch(r"[A-Z0-9]{2,5}(?:/[A-Z0-9]{2,5})*", values[1]):
            raise DataError("ADVISORY_SOURCE_FORMAT")
        seen.add(number)
        kind, action = classify(values[3])
        results.append(
            dict(
                number=number,
                facility=values[1],
                title=values[3],
                kind=kind,
                action=action,
                issued_at=issued.isoformat(),
                utc_date=day.isoformat(),
                html_line=row["html_line"],
                candidate_only=True,
            )
        )
        if len(results) > 1000:
            raise DataError("ADVISORY_RESULT_LIMIT")
    return results


def parse_period(raw, day):
    """
    作用：保守解析 DDHHMM-DDHHMM，按公告日处理相邻月跨日，未知格式保留原文。
    输入：原始有效时间与公告日期。
    输出：UTC 范围或未知，不将有效时间等同于程序影响时段。
    """
    match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", raw)
    if not match:
        return None
    parts = [int(value) for value in match.groups()]
    candidates = []
    for offset in (-1, 0, 1):
        month_index = day.year * 12 + day.month - 1 + offset
        year, month = divmod(month_index, 12)
        for index in (0, 3):
            d, hour, minute = parts[index : index + 3]
            if 1 <= d <= calendar.monthrange(year, month + 1)[1] and hour < 24 and minute < 60:
                candidates.append((index, datetime(year, month + 1, d, hour, minute, tzinfo=UTC)))
    starts = [dt for index, dt in candidates if index == 0 and abs((dt.date() - day).days) <= 2]
    if len(starts) != 1:
        return None
    ends = [
        dt
        for index, dt in candidates
        if index == 3 and starts[0] <= dt <= starts[0] + timedelta(days=7)
    ]
    if len(ends) != 1:
        return None
    return dict(start_utc=starts[0].isoformat(), end_utc=ends[0].isoformat())


def parse_body(raw, candidate):
    """
    作用：提取固定正文并核对清单身份、原文范围及签发时间。
    输入：实际响应和已登记清单候选。
    输出：正文、定位、时间及保守适用说明。
    """
    rows = parse_html(raw)
    headers = [
        compact(c)
        for row in rows
        for c in row["cells"]
        if c["tag"] == "th" and compact(c).startswith("ATCSCC ADVZY ")
    ]
    if len(headers) != 1:
        raise DataError("ADVISORY_SOURCE_FORMAT")
    header = re.fullmatch(r"ATCSCC ADVZY (\d+) (\S+) (\d{2}/\d{2}/\d{4}) (.+)", headers[0])
    if not header:
        raise DataError("ADVISORY_SOURCE_FORMAT")
    number, facility, raw_day, title = header.groups()
    try:
        day = datetime.strptime(raw_day, "%m/%d/%Y").date()
    except ValueError:
        raise DataError("ADVISORY_SOURCE_FORMAT") from None
    if (
        int(number) != candidate["number"]
        or facility != candidate["facility"]
        or day.isoformat() != candidate["utc_date"]
        or title != candidate["title"]
    ):
        raise DataError("ADVISORY_IDENTITY_MISMATCH")
    labels = {}
    for row in rows:
        if len(row["cells"]) == 2:
            label = compact(row["cells"][0])
            if label in ("MESSAGE:", "EFFECTIVE TIME:", "SIGNATURE:"):
                if label in labels:
                    raise DataError("ADVISORY_SOURCE_FORMAT")
                labels[label] = row["cells"][1]
    if (
        set(labels) != {"MESSAGE:", "EFFECTIVE TIME:", "SIGNATURE:"}
        or not labels["MESSAGE:"]["has_pre"]
    ):
        raise DataError("ADVISORY_SOURCE_FORMAT")
    text = "".join(labels["MESSAGE:"]["pre"]).replace("\r\n", "\n").rstrip(" \t\n\xa0")
    if not text or len(text) > 200000 or len(text.splitlines()) > 2000:
        raise DataError("ADVISORY_SOURCE_FORMAT")
    signature = compact(labels["SIGNATURE:"])
    try:
        signed = datetime.strptime(signature, "%y/%m/%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        raise DataError("ADVISORY_SOURCE_FORMAT") from None
    if signed.isoformat() != candidate["issued_at"]:
        raise DataError("ADVISORY_IDENTITY_MISMATCH")
    # 字段的续行原样保留；正文提到别的管制取消时不修改当前公告动作。
    fields, current = {}, None
    for line in text.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9 /_-]*):[ \t]*(.*)$", line)
        if match:
            current = match[1]
            fields.setdefault(current, []).append(match[2])
        elif current:
            fields[current].append(line)
    fields = {key: "\n".join(value) for key, value in fields.items()}
    control = fields.get("CTL ELEMENT")
    if control and control.strip() != facility.split("/")[0]:
        raise DataError("ADVISORY_SCOPE_MISMATCH")
    clauses = [
        line.strip() for line in text.splitlines() if re.search(r"\bONLY\b|MAINLINE|\bSUBS\b", line)
    ]
    codes = sorted(set(re.findall(r"\b([A-Z]{3})\s+MAINLINE\b", text)) - {"ALL", "THE"})
    effective_raw = compact(labels["EFFECTIVE TIME:"])
    return dict(
        text=text,
        fields=fields,
        effective_time_raw=effective_raw,
        effective_window=parse_period(effective_raw, day),
        signature_raw=signature,
        signed_at=signed.isoformat(),
        message_html_line=labels["MESSAGE:"]["pre_html_line"],
        relevance="control_element_confirmed" if control else "facility_candidate_unconfirmed",
        carrier_scope=dict(
            status="restrictive_clause_present" if clauses else "not_explicitly_resolved",
            clauses=[clause[:500] for clause in clauses[:10]],
            excerpted=len(clauses) > 10 or any(len(clause) > 500 for clause in clauses),
            codes_mentioned=codes[:64],
            note="保留原文限制；同文可能涉及多个计划，不能推广为所有航司",
        ),
    )
