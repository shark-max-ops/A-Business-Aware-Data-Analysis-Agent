"""短事务保存运行、工具审计和严格递增事件。"""

import time

from sqlalchemy import literal_column, select

from metric_pilot.agent import models as m
from metric_pilot.agent.titles import clean_title
from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.data.ranges import resolve_range, run_range
from metric_pilot.knowledge.store import KnowledgeStore

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class RunStore(KnowledgeStore):
    """
    作用：持久化分析生命周期
    输入：元数据库引擎
    输出：运行仓储
    """

    def conversation(self):
        """
        作用：建立会话
        输入：无
        输出：服务端会话
        """
        row = {"id": new_id("conv"), "title": "新分析", "created_at": utc_now()}
        with self.write() as c:
            c.execute(m.conversations.insert().values(**row))
        return row

    def require(self, c, ident):
        """
        作用：读取运行
        输入：事务与标识
        输出：记录
        """
        row = c.execute(select(m.runs).where(m.runs.c.id == ident)).mappings().first()
        if row is None:
            raise DataError("RUN_NOT_FOUND")
        return dict(row)

    def public(self, row):
        """
        作用：合并持久状态
        输入：数据库记录
        输出：公开运行
        """
        return {
            "model": "",
            **row["payload"],
            "id": row["id"],
            "status": row["status"],
            "seq": row["seq"],
            "cancel_requested": row["cancel_requested"],
        }

    def read(self, ident):
        """
        作用：恢复运行
        输入：标识
        输出：公开状态
        """
        with self.engine.connect() as c:
            return self.public(self.require(c, ident))

    def event(self, c, ident, kind, payload):
        """
        作用：在同一写事务内分配事件序号
        输入：事务及事件
        输出：无
        """
        row = self.require(c, ident)
        seq = row["seq"] + 1
        c.execute(m.runs.update().where(m.runs.c.id == ident).values(seq=seq))
        c.execute(
            m.events.insert().values(
                run_id=ident, seq=seq, type=kind, payload=payload, created_at=utc_now()
            )
        )

    def emit(self, ident, kind, payload):
        """
        作用：提交进度后供订阅读取
        输入：事件
        输出：无
        """
        with self.write() as c:
            if self.require(c, ident)["status"] not in TERMINAL:
                self.event(c, ident, kind, payload)

    def events_after(self, ident, seq):
        """
        作用：按游标重放事件
        输入：运行及游标
        输出：最多一百条事件
        """
        with self.engine.connect() as c:
            self.require(c, ident)
            return [
                dict(r)
                for r in c.execute(
                    select(m.events)
                    .where(m.events.c.run_id == ident, m.events.c.seq > seq)
                    .order_by(m.events.c.seq)
                    .limit(100)
                ).mappings()
            ]

    def date_range(self, body):
        """
        作用：新运行显式固定范围，续查省略范围时仅继承原调查
        输入：严格运行请求
        输出：服务端规范范围，禁止续查偷偷改变范围
        """
        if body.investigation_id:
            from metric_pilot.agent.investigations import InvestigationStore

            investigation = InvestigationStore(self.engine).read(body.investigation_id)
            inherited = run_range(self.engine, investigation)
            if body.snapshot_id != investigation["snapshot_id"]:
                raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
            if body.date_range is not None and body.date_range != inherited:
                raise DataError("INVESTIGATION_BOUNDARY_MISMATCH")
            return inherited
        return resolve_range(self.engine, body.snapshot_id, body.date_range, require_explicit=True)

    def create(self, body, knowledge_id, budget, model=""):
        """
        作用：绑定运行输入并排队
        输入：已验证请求与固定预算
        输出：运行
        """
        ident = new_id("run")
        body = body.model_copy(update={"date_range": self.date_range(body)})
        payload = {
            **body.model_dump(mode="json"),
            "knowledge_snapshot_id": knowledge_id,
            "budget": budget,
            "result": None,
            "error_code": None,
            "created_at": utc_now(),
            "usage": {},
            "model": model,
        }
        with self.write() as c:
            conv = c.execute(
                select(m.conversations).where(m.conversations.c.id == body.conversation_id)
            ).first()
            if not conv:
                raise DataError("CONVERSATION_NOT_FOUND")
            for previous in body.context_run_ids:
                old = self.public(self.require(c, previous))
                if old["conversation_id"] != body.conversation_id or old["status"] != "completed":
                    raise DataError("INVALID_CONTEXT_RUN")
                if old["snapshot_id"] != body.snapshot_id:
                    raise DataError("CONTEXT_SNAPSHOT_MISMATCH")
                if old["knowledge_snapshot_id"] != knowledge_id:
                    raise DataError("CONTEXT_KNOWLEDGE_MISMATCH")
                if run_range(self.engine, old) != body.date_range:
                    raise DataError("CONTEXT_DATE_RANGE_MISMATCH")
            c.execute(
                m.runs.insert().values(
                    id=ident,
                    conversation_id=body.conversation_id,
                    status="queued",
                    payload=payload,
                    seq=0,
                    cancel_requested=False,
                )
            )
            if body.investigation_id:
                from metric_pilot.agent.investigations import InvestigationStore

                InvestigationStore(self.engine).attach(c, body, knowledge_id, budget, ident)
            c.execute(
                m.conversations.update()
                .where(m.conversations.c.id == body.conversation_id)
                .values(title=" ".join(body.question.split()))
            )
            self.event(c, ident, "run.queued", {"summary": "分析已排队"})
        return self.read(ident)

    def apply_title(self, connection, run, title, expected_title=None):
        """
        作用：只为会话最新运行保存有效标题，避免迟到结果覆盖新话题
        输入：短事务、运行记录、模型标题和可选旧标题
        输出：是否实际更新
        """
        title = clean_title(title)
        if title is None:
            return False
        latest = connection.execute(
            select(m.runs.c.id)
            .where(m.runs.c.conversation_id == run["conversation_id"])
            # SQLite 行插入顺序处理同一时刻创建；随机 ID 不能代表谁更新。
            .order_by(literal_column("rowid").desc())
            .limit(1)
        ).scalar_one()
        if latest != run["id"]:
            return False
        statement = m.conversations.update().where(m.conversations.c.id == run["conversation_id"])
        if expected_title is not None:
            statement = statement.where(m.conversations.c.title == expected_title)
        return connection.execute(statement.values(title=title)).rowcount == 1

    def claim(self):
        """
        作用：串行领取持久任务
        输入：无
        输出：运行或空
        """
        with self.write() as c:
            row = (
                c.execute(
                    select(m.runs)
                    .where(m.runs.c.status == "queued")
                    .order_by(m.runs.c.payload["created_at"].as_string())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            c.execute(m.runs.update().where(m.runs.c.id == row["id"]).values(status="running"))
            self.event(c, row["id"], "run.started", {"summary": "开始分析"})
            return self.public({**row, "status": "running"})

    def finish(
        self,
        ident,
        status,
        result=None,
        error_code=None,
        usage=None,
        conversation_title=None,
        prepared_report=None,
        deadline=None,
        cancel=None,
        error_details=None,
    ):
        """
        作用：原子保存终态与事件
        输入：终态及安全结果
        输出：无
        """
        with self.write() as c:
            row = self.require(c, ident)
            if row["status"] in TERMINAL:
                return
            if row["cancel_requested"] and status != "interrupted":
                status, result, error_code = "cancelled", None, "RUN_CANCELLED"
            # 报告与终态同事务提交；锁等待后再次检查取消及总期限。
            if prepared_report is not None and status == "completed":
                if cancel is not None and cancel.is_set():
                    status, result, error_code = "cancelled", None, "RUN_CANCELLED"
                elif deadline is not None and time.monotonic() >= deadline:
                    status, result, error_code = "failed", None, "RUN_TIMEOUT"
                else:
                    from metric_pilot.reports.service import ReportService

                    if prepared_report["run_id"] != ident or row["status"] != "running":
                        raise DataError("REPORT_RUN_MISMATCH")
                    result = ReportService.persist(c, prepared_report)
                    self.event(c, ident, "report.ready", result)
            safe_details = {
                key: value
                for key, value in (error_details or {}).items()
                if key
                in {
                    "attempts",
                    "status",
                    "failure_kind",
                    "budget_bytes",
                    "fixed_bytes",
                    "latest_group_bytes",
                }
                and type(value) in {str, int}
            }
            payload = {
                "model": "",
                **row["payload"],
                "result": result,
                "error_code": error_code,
                "error_details": safe_details,
                "usage": usage or {},
            }
            c.execute(
                m.runs.update().where(m.runs.c.id == ident).values(status=status, payload=payload)
            )
            if status == "completed":
                self.apply_title(c, row, conversation_title)
            self.event(
                c,
                ident,
                f"run.{status}",
                {
                    "result_kind": result["kind"] if result else None,
                    "result": result,
                    "error_code": error_code,
                    "error_details": safe_details,
                },
            )

    def cancel(self, ident):
        """
        作用：幂等请求取消
        输入：运行标识
        输出：当前状态
        """
        with self.write() as c:
            row = self.require(c, ident)
            if row["status"] not in TERMINAL:
                c.execute(m.runs.update().where(m.runs.c.id == ident).values(cancel_requested=True))
                if row["status"] == "queued":
                    payload = {**row["payload"], "error_code": "RUN_CANCELLED"}
                    c.execute(
                        m.runs.update()
                        .where(m.runs.c.id == ident)
                        .values(status="cancelled", payload=payload)
                    )
                    self.event(c, ident, "run.cancelled", {"error_code": "RUN_CANCELLED"})
        return self.read(ident)

    def tool_start(self, ident, call):
        """
        作用：登记真实工具开始
        输入：运行与调用
        输出：执行 ID
        """
        execution_id = new_id("tool")
        with self.write() as c:
            c.execute(
                m.executions.insert().values(
                    id=execution_id,
                    run_id=ident,
                    tool_call_id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    status="running",
                    result={},
                    duration_ms=0,
                )
            )
            self.event(
                c,
                ident,
                "tool.started",
                {
                    "tool_execution_id": execution_id,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "summary": "正在执行工具",
                },
            )
        return execution_id

    def tool_finish(self, ident, execution_id, name, result, duration_ms):
        """
        作用：配对工具结束与结果
        输入：工具反馈
        输出：无
        """
        status = (
            "succeeded"
            if result.ok
            else ("cancelled" if result.error_code == "RUN_CANCELLED" else "failed")
        )
        with self.write() as c:
            c.execute(
                m.executions.update()
                .where(m.executions.c.id == execution_id)
                .values(status=status, result=result.model_dump(), duration_ms=duration_ms)
            )
            self.event(
                c,
                ident,
                "tool.finished",
                {
                    "tool_execution_id": execution_id,
                    "tool": name,
                    "status": status,
                    **result.model_dump(),
                },
            )

    def recover(self):
        """
        作用：把重启遗留运行标为中断
        输入：无
        输出：无
        """
        with self.write() as c:
            rows = list(c.execute(select(m.runs).where(m.runs.c.status == "running")).mappings())
            for row in rows:
                pending = (
                    c.execute(
                        select(m.executions).where(
                            m.executions.c.run_id == row["id"], m.executions.c.status == "running"
                        )
                    )
                    .mappings()
                    .all()
                )
                for tool in pending:
                    result = {
                        "ok": False,
                        "summary": "服务重启，工具已中断",
                        "data": {},
                        "artifact_ids": [],
                        "error_code": "RUN_INTERRUPTED",
                        "retryable": False,
                    }
                    c.execute(
                        m.executions.update()
                        .where(m.executions.c.id == tool["id"])
                        .values(status="interrupted", result=result)
                    )
                    self.event(
                        c,
                        row["id"],
                        "tool.finished",
                        {
                            "tool_execution_id": tool["id"],
                            "tool": tool["name"],
                            "status": "interrupted",
                            **result,
                        },
                    )
                c.execute(
                    m.runs.update()
                    .where(m.runs.c.id == row["id"])
                    .values(
                        status="interrupted",
                        payload={**row["payload"], "error_code": "RUN_INTERRUPTED"},
                    )
                )
                self.event(c, row["id"], "run.interrupted", {"error_code": "RUN_INTERRUPTED"})
