"""可解释的检索重排与逐字摘录；不生成答案或修改规则。"""

import re

from metric_pilot.knowledge.documents import FIELD_SYNONYMS, expand_query, focused_fields


def is_substantive(piece, query):
    """
    作用：解释类问题不返回孤立标题、坐标和纯映射提示
    输入：片段与查询
    输出：是否参与结果展示
    """
    text = piece["text"].strip()
    if re.fullmatch(r"#{1,6}[^\n]*", text):
        return False
    metadata_query = any(word in query for word in ("来源", "出处", "版本", "映射", "哈希", "网址"))
    if not metadata_query and (text.startswith("原文定位：") or text.startswith("项目映射：")):
        return False
    return True


def relevance_bonus(piece, query):
    """
    作用：优先直接匹配字段的解释，口径问题则保留项目规则优先级
    输入：真实片段和原查询
    输出：重排加分与人类可读的匹配说明
    """
    fields = focused_fields(query)
    section = piece["locator"].get("section", "").strip("# ")
    policy = any(
        word in query
        for word in ("分母", "分子", "口径", "平均", "占比", "怎么算", "计算", "延误率", "异常")
    )
    direct = any(
        section.lower() in {field.lower(), *(x.lower() for x in FIELD_SYNONYMS[field])}
        for field in fields
    )
    bonus = 0.0
    if direct:
        bonus += 0.005 if policy else 0.04
    if direct and not policy and piece["source_kind"] == "official_reference":
        bonus += 0.008
    if policy and piece["source_kind"] == "project_policy":
        bonus += 0.012
    if direct and not policy:
        reason = "直接匹配字段释义"
    elif policy and piece["source_kind"] == "project_policy":
        reason = "相关项目计算口径"
    else:
        reason = "相关资料片段"
    return bonus, reason


def focused_excerpt(text, query, limit=320):
    """
    作用：从原片段截取相关句子，保持逐字可回查而非生成摘要
    输入：原文、查询与字符上限
    输出：原文中的连续子串
    """
    if len(text) <= limit:
        return text
    if text.lstrip().startswith("|"):
        # 短表格完整保留；长表格由前端展开原文查看，不拼造表格行。
        if len(text) <= 900:
            return text
        return text[:limit].rsplit("\n", 1)[0] or text[:limit]
    terms = re.findall(r"[a-zA-Z_]{3,}", expand_query(query).lower())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    sentences = list(re.finditer(r"[^。！？\n]+[。！？]?|[^\n]+", text))
    best_start, best_score = 0, -1
    for sentence in sentences:
        value = sentence.group().lower()
        score = sum(term in value for term in terms) + sum(term in value for term in chinese)
        if score > best_score:
            best_start, best_score = sentence.start(), score
    start = min(best_start, max(0, len(text) - limit))
    return text[start : start + limit]
