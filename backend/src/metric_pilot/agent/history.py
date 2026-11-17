"""Deterministic investigation navigation; matching never grants evidence permission."""

import json
import re
import unicodedata

from sqlalchemy import select

from metric_pilot.agent.investigations import InvestigationStore, authorized_runs
from metric_pilot.data.common import DataError
from metric_pilot.data.models import ArtifactRecord, QueryExecution


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def match_score(query, value):
    text = normalize(value)
    terms = re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+(?:[-:][a-z0-9_]+)*", normalize(query))
    words = set(re.findall(r"[a-z0-9_]+(?:[-:][a-z0-9_]+)*", text))
    exact, covered, total = 0, 0, 0
    for term in terms:
        chinese = bool(re.search(r"[\u3400-\u9fff]", term))
        exact += int(term in text if chinese else term in words)
        if chinese:
            pairs = {term[i : i + 2] for i in range(len(term) - 1)}
            covered += sum(pair in text for pair in pairs)
            total += len(pairs)
    if not exact and not covered:
        return None
    return (int(exact == len(terms)), exact, covered / max(1, total))


def matching_excerpt(query, fields, width=180):
    """Locate matches in normalized text, then slice the original field verbatim."""
    terms = re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+(?:[-:][a-z0-9_]+)*", normalize(query))
    for fallback in (False, True):
        for name, value in fields.items():
            original = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            normalized, positions = "", []
            for index, char in enumerate(original):
                for unit in unicodedata.normalize("NFKC", char).casefold():
                    if unit.isspace():
                        if not normalized or normalized[-1] == " ":
                            continue
                        unit = " "
                    normalized += unit
                    positions.append(index)
            needles = (
                [
                    pair
                    for term in terms
                    if re.search(r"[\u3400-\u9fff]", term)
                    for pair in [term[i : i + 2] for i in range(len(term) - 1)]
                ]
                if fallback
                else terms
            )
            matches = []
            for needle in needles:
                pattern = re.escape(needle)
                if not re.search(r"[\u3400-\u9fff]", needle):
                    pattern = r"(?<![a-z0-9_:-])" + pattern + r"(?![a-z0-9_:-])"
                match = re.search(pattern, normalized)
                if match:
                    matches.append(match.start())
            if matches:
                at = positions[min(matches)]
                start = max(0, at - width // 3)
                end = min(len(original), start + width)
                return (
                    f"{name}: "
                    + ("…" if start else "")
                    + original[start:end]
                    + ("…" if end < len(original) else "")
                )
    return ""


def bound_search_reply(result, limit=4096):
    result = {**result, "omitted": result.get("omitted", False), "omission_reasons": []}
    candidates = result["candidates"]

    def size():
        result["returned_count"] = len(candidates)
        return len(json.dumps(result, ensure_ascii=False).encode())

    def omitted(reason):
        result["omitted"] = True
        if reason not in result["omission_reasons"]:
            result["omission_reasons"].append(reason)

    if result["matched_count"] > len(candidates):
        omitted("candidate_limit")
    if size() > limit:
        for item in candidates:
            if len(item.get("excerpt", "")) > 80:
                label, _, text = item["excerpt"].partition(": ")
                item["excerpt"] = (
                    matching_excerpt(result.get("query", ""), {label: text}, width=40)
                    or item["excerpt"][:80] + "…"
                )
        omitted("excerpt_shortened")
    optional = (
        "columns",
        "exploration_scope",
        "metric_revision_ids",
        "date_range",
        "query_purpose",
        "matched_fields",
        "created_at",
    )
    for key in optional:
        if size() <= limit:
            break
        for item in candidates:
            if key in item:
                item.pop(key)
                item.setdefault("omitted_fields", []).append(key)
        omitted("optional_metadata_omitted")
    while size() > limit and len(candidates) > 1:
        candidates.pop()
        omitted("reply_capacity")
    if size() > limit:
        raise DataError("HISTORY_REPLY_CAPACITY_EXCEEDED")
    return result


def corrected_findings(findings):
    successors = {f["supersedes"]: f["id"] for f in findings if f.get("supersedes")}
    statuses = {f["id"]: f.get("source_run_status") for f in findings}
    result = []
    for item in findings:
        current, visited = item["id"], set()
        while current in successors and current not in visited:
            visited.add(current)
            current = successors[current]
        result.append(
            {
                **item,
                "superseded": item["id"] in successors,
                "direct_successor_id": successors.get(item["id"]),
                "effective_successor_id": current if current != item["id"] else None,
                "direct_successor_source_run_status": statuses.get(successors.get(item["id"])),
                "effective_successor_source_run_status": statuses.get(current)
                if current != item["id"]
                else None,
            }
        )
    return result


def read_history(registry, args):
    engine, run = registry.store.engine, registry.run
    allowed = authorized_runs(engine, run)
    investigation = InvestigationStore(engine).read(run["investigation_id"])
    findings = corrected_findings(investigation["findings"])
    if args.finding_id is not None:
        selected = [f for f in findings if f["id"] == args.finding_id]
        if not selected:
            raise DataError("FINDING_NOT_FOUND")
        return {"findings": selected, "finding_count": len(findings), "offset": 0}
    with engine.connect() as connection:
        records = (
            connection.execute(
                select(
                    ArtifactRecord.id,
                    ArtifactRecord.run_id,
                    ArtifactRecord.payload,
                    QueryExecution.sql,
                )
                .join(QueryExecution, QueryExecution.id == ArtifactRecord.execution_id)
                .where(ArtifactRecord.run_id.in_(allowed), QueryExecution.status == "succeeded")
            )
            .mappings()
            .all()
        )
    artifacts = {r["id"]: r for r in records}
    candidates = []
    for f in findings:
        fields = {key: f[key] for key in ("check", "text", "questions")}
        candidates.append(
            (
                {
                    "type": "finding",
                    "scope_context": f.get("scope_context"),
                    **{
                        k: f[k]
                        for k in (
                            "id",
                            "run_id",
                            "source_run_status",
                            "kind",
                            "verification",
                            "superseded",
                            "direct_successor_id",
                            "effective_successor_id",
                            "direct_successor_source_run_status",
                            "effective_successor_source_run_status",
                            "created_at",
                        )
                    },
                    "artifacts": [
                        {
                            "artifact_id": ident,
                            "readable": "requires_read_artifact_validation"
                            if ident in artifacts
                            else False,
                        }
                        for ident in f["artifact_ids"]
                    ],
                },
                fields,
            )
        )
    for r in records:
        p = r["payload"]
        fields = {
            k: p.get(k)
            for k in (
                "columns",
                "query_purpose",
                "metric_revision_ids",
                "date_range",
                "exploration_scope",
            )
        }
        fields["sql"] = r["sql"][:1500]
        candidates.append(
            (
                {
                    "type": "artifact",
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "readable": "requires_read_artifact_validation",
                    "verification": "frozen_query_only",
                    "source_run_status": "current" if r["run_id"] == run["id"] else "completed",
                    **fields,
                    "rows": p.get("returned_rows"),
                    "truncated": p.get("truncated"),
                    "created_at": p.get("created_at", ""),
                },
                fields,
            )
        )
    hits = []
    for item, fields in candidates:
        rendered = {key: json.dumps(value, ensure_ascii=False) for key, value in fields.items()}
        score = match_score(args.query, " ".join(rendered.values()))
        if score:
            matched = [key for key, value in rendered.items() if match_score(args.query, value)]
            hits.append(
                (
                    score,
                    {
                        **item,
                        "matched_fields": matched,
                        "excerpt": matching_excerpt(args.query, {k: fields[k] for k in matched}),
                    },
                )
            )
    hits.sort(key=lambda h: h[1]["id"])
    hits.sort(key=lambda h: (h[0], h[1]["created_at"]), reverse=True)
    result = {
        "query": args.query,
        "candidates": [h[1] for h in hits[:3]],
        "omitted": len(hits) > 3,
        "matched_count": len(hits),
        "note": "本次匹配未找到不代表以前没有查过；候选和摘要不扩大证据权限；"
        "元数据候选须read_artifact完成范围、外部绑定及文件哈希校验。",
    }
    for candidate in result["candidates"]:
        candidate.pop("sql", None)
    return bound_search_reply(result)


def navigation(current, findings, prior, visible=()):
    """Bound identity navigation without copying result tables or granting permissions."""
    result = {
        "items": [],
        "omitted": False,
        "note": "更多历史用read_investigation(query=主题)；已知ID直接读取。",
    }
    seen = set(visible)
    keys = {
        "id",
        "artifact_id",
        "run_id",
        "columns",
        "rows",
        "truncated",
        "query_purpose",
        "metric_revision_ids",
        "date_range",
        "exploration_scope",
        "verification",
        "source_run_status",
        "kind",
        "superseded",
        "direct_successor_id",
        "effective_successor_id",
        "direct_successor_source_run_status",
        "effective_successor_source_run_status",
        "questions",
        "check",
    }
    pending = []
    priority = {}
    used = [0, 0, 0]
    quotas = [768, 384, 384]
    for category, item in (
        [(0, i) for i in reversed(current)] + [(1, i) for i in findings] + [(2, i) for i in prior]
    ):
        ident = item.get("artifact_id") or item.get("id")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        entry = {key: value for key, value in item.items() if key in keys}
        priority[id(entry)] = len(priority)
        for key in ("columns", "questions", "check", "exploration_scope", "metric_revision_ids"):
            if len(json.dumps(entry, ensure_ascii=False).encode()) <= quotas[category]:
                break
            if key in entry:
                entry.pop(key)
                entry.setdefault("omitted_fields", []).append(key)
                result["omitted"] = True
        size = len(json.dumps(entry, ensure_ascii=False).encode()) + 2
        if used[category] + size <= quotas[category]:
            used[category] += size
            result["items"].append(entry)
        else:
            pending.append(entry)
    for entry in pending:
        if (
            len(
                json.dumps(
                    {**result, "items": result["items"] + [entry]}, ensure_ascii=False
                ).encode()
            )
            <= 1536
        ):
            result["items"].append(entry)
        else:
            result["omitted"] = True
    result["items"].sort(key=lambda entry: priority[id(entry)])
    while len(json.dumps(result, ensure_ascii=False).encode()) > 1536 and result["items"]:
        result["items"].pop()
        result["omitted"] = True
    return result


def visible_locators(messages):
    """Only structured displayed identities count; incidental text does not hide navigation."""
    locators, complete = set(), set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            data = json.loads(message["content"]).get("data", {})
        except (ValueError, TypeError):
            continue
        artifact = data.get("artifact", {})
        ident = artifact.get("id")
        if ident:
            locators.add(ident)
            rows = data.get("indexed_rows", [])
            if (
                not artifact.get("truncated")
                and not data.get("context_rows_omitted")
                and [r[0] for r in rows] == list(range(artifact.get("returned_rows", -1)))
            ):
                complete.add(ident)
        locators.update(f["id"] for f in data.get("findings", []) if f.get("id"))
        locators.update(f["id"] for f in data.get("candidates", []) if f.get("id"))
    return locators, complete


def complete_caches(summaries, visible):
    selected = []
    for item in reversed(summaries):
        preview = item.get("result_preview", {})
        if (
            item.get("artifact_id") in visible
            or item.get("truncated")
            or not preview.get("complete")
            or len(preview.get("rows", [])) != item.get("rows")
        ):
            continue
        candidate = {k: v for k, v in item.items() if k != "sql"}
        candidate["result_preview"] = {**preview, "row_indices": list(range(len(preview["rows"])))}
        if len(json.dumps(selected + [candidate], ensure_ascii=False).encode()) <= 4096:
            selected.append(candidate)
        if len(selected) == 2:
            break
    return selected
