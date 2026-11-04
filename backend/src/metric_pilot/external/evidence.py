"""5.C 服务端外部证据登记、版本授权与固定查询快照；不向 Agent 开放工具。"""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select

from metric_pilot.agent.investigations import authorized_runs
from metric_pilot.agent.store import RunStore
from metric_pilot.data.common import DataError, canonical_bytes, digest, new_id, utc_now
from metric_pilot.external import models as m
from metric_pilot.external.advisories import AdvisoryStore
from metric_pilot.external.advisory_parser import parse_body
from metric_pilot.external.weather_proof import verify_weather
from metric_pilot.external.weather_snapshot import build_weather_database
from metric_pilot.knowledge.store import KnowledgeStore


class ExternalEvidenceService(KnowledgeStore):
    """
    作用：固定外部原件与解析版本，并按真实运行/调查边界授权使用。
    输入：元数据库、证据存储根及可选服务端采集根。
    输出：可追溯证据和独立天气快照，不替换主数据快照。
    """

    def __init__(self, engine, storage: Path, *, import_storage: Path | None = None):
        """
        作用：固定服务端路径，模型或资料不能传入存储根。
        输入：引擎、工作区根及可选采集原件根。
        输出：无。
        """
        super().__init__(engine)
        self.storage = storage.resolve()
        self.import_storage = (import_storage or storage).resolve()
        self.root = self.storage / "external" / "frozen"

    def path(self, root, name):
        """
        作用：限制服务端 ID 或摘要导出的路径，阻止越界链接。
        输入：固定根和内部文件名。
        输出：受限路径。
        """
        result = root / name
        if not result.resolve().is_relative_to(root.resolve()):
            raise DataError("EXTERNAL_FILE_UNAVAILABLE")
        return result

    def run(self, run_id, *, writable=False, connection=None):
        """
        作用：只从数据库恢复运行，不信任调用者提交的快照或调查身份。
        输入：运行标识、是否写入和可选事务。
        输出：真实运行；终态或取消不允许新增证据。
        """
        if connection is None:
            with self.engine.connect() as c:
                return self.run(run_id, writable=writable, connection=c)
        store = RunStore(self.engine)
        run = store.public(store.require(connection, run_id))
        if writable and (run["status"] != "running" or run["cancel_requested"]):
            raise DataError("EXTERNAL_RUN_NOT_WRITABLE")
        return run

    def read_fetch(self, record, url, params):
        """
        作用：核对采集清单和原件，失败或截断不能升级为正式证据。
        输入：抓取记录和服务端预期 URL/参数。
        输出：真实原件字节。
        """
        if (
            not isinstance(record, dict)
            or not re.fullmatch(r"fetch_[a-f0-9]{32}", record.get("id", ""))
            or record.get("url") != url
            or record.get("params") != params
            or record.get("status") != "succeeded"
            or record.get("truncated")
            or record.get("http_status") not in (200, 204)
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", ""))
        ):
            raise DataError("EXTERNAL_SOURCE_INVALID")
        root = self.import_storage / "external" / "fetches"
        try:
            meta = json.loads(self.path(root, record["id"] + ".json").read_bytes())
            path = self.path(root, record["id"] + ".raw")
            if path.stat().st_size > 64 * 1024 * 1024:
                raise DataError("EXTERNAL_SOURCE_LIMIT")
            raw = path.read_bytes()
            if (
                meta != record
                or len(raw) != record["bytes"]
                or hashlib.sha256(raw).hexdigest() != record["sha256"]
            ):
                raise DataError("EXTERNAL_SOURCE_CHANGED")
            return raw
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise DataError("EXTERNAL_SOURCE_CHANGED") from None

    def retain(self, originals):
        """
        作用：将已核对原件按内容哈希另存，后续不依赖可编辑的采集结果 JSON。
        输入：抓取记录和真实字节。
        输出：固定抓取元数据副本。
        """
        root = self.root / "raw"
        root.mkdir(parents=True, exist_ok=True)
        records = []
        for record, raw in originals:
            path = self.path(root, record["sha256"] + ".raw")
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                    raise DataError("EXTERNAL_SOURCE_CHANGED")
            else:
                with path.open("xb") as stream:
                    stream.write(raw)
            records.append(record)
        return records

    def record(self, evidence_id, connection=None):
        """
        作用：读取数据库锚定的不可变证据并检查内容摘要。
        输入：证据标识和可选连接。
        输出：证据记录。
        """
        if not re.fullmatch(r"xe_[a-f0-9]{32}", evidence_id):
            raise DataError("EXTERNAL_EVIDENCE_NOT_ALLOWED")
        if connection is None:
            with self.engine.connect() as c:
                return self.record(evidence_id, c)
        row = (
            connection.execute(select(m.evidence).where(m.evidence.c.id == evidence_id))
            .mappings()
            .first()
        )
        if row is None:
            raise DataError("EXTERNAL_EVIDENCE_NOT_ALLOWED")
        if digest(row["payload"]) != row["content_hash"] or row["payload"]["id"] != row["id"]:
            raise DataError("EXTERNAL_EVIDENCE_CHANGED")
        return dict(row)

    def authorize(self, run, evidence_id):
        """
        作用：校验同运行或同调查已完成来源，以及当前运行选定的外部版本。
        输入：数据库恢复的运行和证据标识。
        输出：授权且原件完整的固定证据。
        """
        row = self.record(evidence_id)
        payload = row["payload"]
        allowed = authorized_runs(self.engine, run)
        if (
            row["owner_run_id"] not in allowed
            or payload["snapshot_id"] != run["snapshot_id"]
            or payload["knowledge_snapshot_id"] != run["knowledge_snapshot_id"]
            or payload["conversation_id"] != run["conversation_id"]
        ):
            raise DataError("EXTERNAL_EVIDENCE_NOT_ALLOWED")
        with self.engine.connect() as c:
            owner = c.execute(
                select(m.bindings).where(
                    m.bindings.c.run_id == row["owner_run_id"],
                    m.bindings.c.evidence_id == evidence_id,
                )
            ).first()
            selected = c.execute(
                select(m.bindings.c.evidence_id).where(
                    m.bindings.c.run_id == run["id"], m.bindings.c.slot == row["slot"]
                )
            ).scalar()
        if owner is None or selected not in (None, evidence_id):
            raise DataError("EXTERNAL_VERSION_MISMATCH")
        for record in payload["fetches"]:
            path = self.path(self.root / "raw", record["sha256"] + ".raw")
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]
            ):
                raise DataError("EXTERNAL_SOURCE_CHANGED")
        return payload

    def existing(self, run, slot, capture_hash):
        """
        作用：同一运行的同一条件固定一个外部版本，重复登记幂等但不能替换。
        输入：真实运行、条件槽和采集摘要。
        输出：已有证据或空值。
        """
        with self.engine.connect() as c:
            identifier = c.execute(
                select(m.bindings.c.evidence_id).where(
                    m.bindings.c.run_id == run["id"], m.bindings.c.slot == slot
                )
            ).scalar()
        if identifier is None:
            return None
        payload = self.authorize(run, identifier)
        if payload["capture_hash"] != capture_hash:
            raise DataError("EXTERNAL_VERSION_MISMATCH")
        return payload

    def publish(self, run, payload, snapshot=None):
        """
        作用：在同一事务重新校验运行并登记证据、快照与不可替换的版本绑定。
        输入：原始运行、服务端证据和可选天气快照。
        输出：固定证据。
        """
        with self.write() as c:
            current = self.run(run["id"], writable=True, connection=c)
            if (current["snapshot_id"], current["knowledge_snapshot_id"]) != (
                run["snapshot_id"],
                run["knowledge_snapshot_id"],
            ):
                raise DataError("EXTERNAL_CONTEXT_MISMATCH")
            if c.execute(
                select(m.bindings.c.evidence_id).where(
                    m.bindings.c.run_id == run["id"], m.bindings.c.slot == payload["slot"]
                )
            ).first():
                raise DataError("EXTERNAL_VERSION_MISMATCH")
            c.execute(
                m.evidence.insert().values(
                    id=payload["id"],
                    owner_run_id=run["id"],
                    kind=payload["kind"],
                    slot=payload["slot"],
                    payload=payload,
                    content_hash=digest(payload),
                    created_at=payload["created_at"],
                )
            )
            if snapshot:
                c.execute(
                    m.snapshots.insert().values(
                        id=snapshot["id"],
                        evidence_id=payload["id"],
                        payload=snapshot,
                        content_hash=digest(snapshot),
                    )
                )
            c.execute(
                m.bindings.insert().values(
                    run_id=run["id"], evidence_id=payload["id"], slot=payload["slot"]
                )
            )
        return payload

    def base_payload(self, run, kind, slot, capture_hash, fetches):
        """
        作用：由服务端填写证据身份、主边界与原件信息。
        输入：真实运行、类型、条件槽、摘要和原件。
        输出：新证据基础内容。
        """
        return dict(
            id=new_id("xe"),
            kind=kind,
            slot=slot,
            capture_hash=capture_hash,
            snapshot_id=run["snapshot_id"],
            knowledge_snapshot_id=run["knowledge_snapshot_id"],
            conversation_id=run["conversation_id"],
            created_at=utc_now(),
            fetches=fetches,
        )

    async def freeze_weather(self, run_id, weather_id):
        """
        作用：从真实原件复核天气，再冻结证据及允许查询的独立数据快照。
        输入：真实运行与采集结果标识，不接受模型数值或文件路径。
        输出：服务端外部证据和快照标识。
        """
        run = self.run(run_id, writable=True)
        if not re.fullmatch(r"weather_[a-f0-9]{32}", weather_id):
            raise DataError("EXTERNAL_SOURCE_INVALID")
        path = self.path(self.import_storage / "external" / "weather", weather_id + ".json")
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                raise DataError("EXTERNAL_SOURCE_LIMIT")
            weather = json.loads(path.read_bytes())
            if weather["id"] != weather_id:
                raise DataError("EXTERNAL_SOURCE_INVALID")
            async with asyncio.timeout(120):
                originals = await verify_weather(weather, self.read_fetch)
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise DataError("EXTERNAL_SOURCE_INVALID") from None
        except DataError:
            raise
        except ValueError:
            raise DataError("EXTERNAL_DERIVATION_MISMATCH") from None
        except TimeoutError:
            raise DataError("EXTERNAL_TIMEOUT") from None
        slot = digest(
            dict(
                kind="weather",
                source=weather["source"],
                airport=weather["mapping"]["airport"],
                start=weather["local_start"],
                end=weather["local_end"],
                fields=sorted(weather["coverage"]),
            )
        )
        capture_hash = digest(weather)
        found = self.existing(run, slot, capture_hash)
        if found:
            return found
        fetches = self.retain(originals)
        payload = self.base_payload(run, "weather", slot, capture_hash, fetches)
        snapshot_id = new_id("xsnap")
        folder = self.path(self.root / "snapshots", snapshot_id)
        snapshot = build_weather_database(folder, snapshot_id, payload["id"], weather)
        payload.update(
            capture_id=weather_id,
            external_snapshot_id=snapshot_id,
            document=weather,
            published_at=None,
            effective_time=None,
            locator=dict(
                kind="weather_observation",
                utc_start=weather["utc_start"],
                utc_end_exclusive=weather["utc_end_exclusive"],
            ),
        )
        return self.publish(run, payload, snapshot)

    async def freeze_advisory(self, run_id, body_ref):
        """
        作用：只冻结已经读取的固定正文，清单候选不能成为正文证据。
        输入：真实运行与固定正文标识。
        输出：新版本证据；不发送网络请求。
        """
        run = self.run(run_id, writable=True)
        store = AdvisoryStore(self.import_storage)
        saved = store.load(body_ref, "advbody")
        candidate, _ = store.candidate(saved["candidate_ref"])
        from metric_pilot.external.advisories import body_params

        raw = store.raw(saved["fetch"], "/adv_otherdis", body_params(candidate))
        parsed = parse_body(raw, candidate)
        if any(
            parsed[key] != saved["document"][key]
            for key in ("text", "effective_time_raw", "signature_raw", "message_html_line")
        ):
            raise DataError("EXTERNAL_SOURCE_CHANGED")
        slot = digest(
            dict(
                kind="advisory",
                date=candidate["utc_date"],
                number=candidate["number"],
                facility=candidate["facility"],
            )
        )
        capture_hash = digest(saved)
        found = self.existing(run, slot, capture_hash)
        if found:
            return found
        payload = self.base_payload(
            run, "advisory", slot, capture_hash, self.retain([(saved["fetch"], raw)])
        )
        payload.update(
            capture_id=body_ref,
            external_snapshot_id=None,
            document=parsed,
            candidate=candidate,
            published_at=parsed["signed_at"],
            effective_time=parsed["effective_window"],
            locator=dict(
                kind="advisory_message",
                html_line=parsed["message_html_line"],
                line_start=1,
                line_end=len(parsed["text"].splitlines()),
            ),
        )
        return self.publish(run, payload)

    def bind_existing(self, run_id, evidence_id):
        """
        作用：显式复用同调查已完成旧运行的固定外部版本，禁止跨调查或失败来源。
        输入：当前运行和旧证据。
        输出：已绑定证据。
        """
        run = self.run(run_id, writable=True)
        payload = self.authorize(run, evidence_id)
        with self.write() as c:
            self.run(run_id, writable=True, connection=c)
            existing = c.execute(
                select(m.bindings.c.evidence_id).where(
                    m.bindings.c.run_id == run_id, m.bindings.c.slot == payload["slot"]
                )
            ).scalar()
            if existing not in (None, evidence_id):
                raise DataError("EXTERNAL_VERSION_MISMATCH")
            if existing is None:
                c.execute(
                    m.bindings.insert().values(
                        run_id=run_id, evidence_id=evidence_id, slot=payload["slot"]
                    )
                )
        return payload

    def weather_snapshot(self, run, snapshot_id, *, bound_run=None):
        """
        作用：验证外部快照、证据权限、执行运行绑定及文件/结构摘要。
        输入：真实消费运行、外部快照和需要绑定的产物所属运行。
        输出：快照元数据及服务端只读数据库路径。
        """
        if not re.fullmatch(r"xsnap_[a-f0-9]{32}", snapshot_id):
            raise DataError("EXTERNAL_SNAPSHOT_NOT_ALLOWED")
        with self.engine.connect() as c:
            row = (
                c.execute(select(m.snapshots).where(m.snapshots.c.id == snapshot_id))
                .mappings()
                .first()
            )
            if row is None or digest(row["payload"]) != row["content_hash"]:
                raise DataError("EXTERNAL_SNAPSHOT_NOT_ALLOWED")
            snapshot = row["payload"]
            attached = c.execute(
                select(m.bindings).where(
                    m.bindings.c.run_id == (bound_run or run["id"]),
                    m.bindings.c.evidence_id == row["evidence_id"],
                )
            ).first()
        payload = self.authorize(run, row["evidence_id"])
        if (
            attached is None
            or snapshot["id"] != snapshot_id
            or payload["external_snapshot_id"] != snapshot_id
            or snapshot["evidence_id"] != payload["id"]
            or digest(snapshot["schema"]) != snapshot["schema_hash"]
        ):
            raise DataError("EXTERNAL_VERSION_MISMATCH")
        folder = self.path(self.root / "snapshots", snapshot_id)
        path = self.path(folder, "analytics.duckdb")
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["database_sha256"]
        ):
            raise DataError("EXTERNAL_SNAPSHOT_CHANGED")
        return snapshot, path

    def query_target(self, ctx, snapshot_id):
        """
        作用：外部 SQL 只使用当前真实运行明确绑定的版本和服务器路径。
        输入：可信查询上下文和外部标识。
        输出：已授权快照和路径。
        """
        run = self.run(ctx.run_id, writable=True)
        if (ctx.snapshot_id, ctx.knowledge_snapshot_id) != (
            run["snapshot_id"],
            run["knowledge_snapshot_id"],
        ):
            raise DataError("EXTERNAL_CONTEXT_MISMATCH")
        return self.weather_snapshot(run, snapshot_id)

    def validate_artifact(self, run, artifact):
        """
        作用：验证查询产物的主快照及独立外部版本，不接受删除外部标识绕过。
        输入：真实消费运行与成功产物。
        输出：外部快照，普通本地查询返回空值。
        """
        snapshot_id = artifact.get("external_snapshot_id")
        if not snapshot_id:
            if (
                artifact.get("external_evidence_ids")
                or "v_weather_observations" in artifact["tables"]
            ):
                raise DataError("EXTERNAL_VERSION_MISMATCH")
            return None
        if artifact["snapshot_id"] != run["snapshot_id"] or artifact["metric_revision_ids"]:
            raise DataError("EXTERNAL_CONTEXT_MISMATCH")
        snapshot, _ = self.weather_snapshot(run, snapshot_id, bound_run=artifact["run_id"])
        if artifact["external_evidence_ids"] != [snapshot["evidence_id"]]:
            raise DataError("EXTERNAL_VERSION_MISMATCH")
        return snapshot

    def collect(self, run, evidence_ids):
        """
        作用：在报告准备时冻结授权的原文副本，旧报告读取不再访问当前文件。
        输入：真实运行与实际引用的外部证据集合。
        输出：FrozenSource 字典。
        """
        actual = self.run(run["id"])
        if actual["status"] not in ("running", "completed"):
            raise DataError("EXTERNAL_RUN_NOT_WRITABLE")
        if (actual["snapshot_id"], actual["knowledge_snapshot_id"]) != (
            run["snapshot_id"],
            run["knowledge_snapshot_id"],
        ):
            raise DataError("EXTERNAL_CONTEXT_MISMATCH")
        result = {}
        for ident in sorted(evidence_ids):
            payload = self.authorize(actual, ident)
            if payload["kind"] == "advisory":
                title = (
                    f"FAA ATCSCC #{payload['candidate']['number']:03d} "
                    f"{payload['candidate']['title']}"
                )
                content = payload["document"]["text"]
                kind = "official_advisory"
            else:
                weather = payload["document"]
                title = (
                    f"{weather['mapping']['airport']} 天气观测 {weather['local_start']} "
                    f"至 {weather['local_end']}"
                )
                content = (
                    "固定天气原始观测（非已审核项目指标）；缺失不补零，降水不累计。\n"
                    + canonical_bytes(
                        dict(
                            mapping=weather["mapping"],
                            coverage=weather["coverage"],
                            observations=weather["observations"],
                        )
                    ).decode("utf-8")
                )
                kind = "official_weather_observations"
            result[ident] = dict(
                id=ident,
                revision_id=ident,
                title=title,
                source_kind=kind,
                content=content,
                locator={
                    **payload["locator"],
                    "fetches": payload["fetches"],
                    "content_hash": digest(payload),
                    "capture_id": payload["capture_id"],
                    "published_at": payload["published_at"],
                    "effective_time": payload["effective_time"],
                    "external_snapshot_id": payload["external_snapshot_id"],
                },
            )
        return result
