"""SQLite 持久化串行任务；重启不冒充恢复已中断模型调用。"""

from threading import Event, Thread

from sqlalchemy import select

from metric_pilot.data.common import DataError, utc_now
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.documents import DocumentService
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.wiki import WikiService


class KnowledgeWorker:
    """
    作用：监督文档索引和 Wiki 后台任务
    输入：仓储与模型依赖
    输出：串行 worker
    """

    def __init__(self, store, provider):
        """
        作用：组合服务并建立停止信号
        输入：仓储、模型适配器
        输出：无
        """
        self.store, self.provider = store, provider
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name="knowledge-worker", daemon=True)

    def start(self):
        """
        作用：将崩溃遗留任务标为失败再启动串行循环
        输入：无
        输出：无
        """
        with self.store.write() as connection:
            connection.execute(
                m.jobs.update()
                .where(m.jobs.c.state == "running")
                .values(state="failed", stage="interrupted", error_code="WORKER_INTERRUPTED")
            )
        self.thread.start()

    def close(self):
        """
        作用：停止领取新任务并有界等待当前调用
        输入：无
        输出：无
        """
        self.stop_event.set()
        self.thread.join(timeout=60)

    def step(self):
        """
        作用：事务领取一个任务，网络调用期间不持有写事务
        输入：无
        输出：是否处理过任务
        """
        with self.store.write() as connection:
            row = (
                connection.execute(
                    select(m.jobs)
                    .where(m.jobs.c.state == "queued")
                    .order_by(m.jobs.c.created_at, m.jobs.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            job = dict(row)
            connection.execute(
                m.jobs.update()
                .where(m.jobs.c.id == job["id"])
                .values(
                    state="running", stage="parsing" if job["kind"] == "document" else "generating"
                )
            )
        try:
            if job["kind"] == "document":
                count = DocumentService(self.store).index_revision(
                    job["payload"]["revision_id"], job["id"]
                )
                result = {
                    "chunks": count,
                    "index_mode": "text",
                    "embedding_status": "not_requested",
                }
            elif job["kind"] == "embedding":
                result = Retriever(self.store, self.provider).index_embeddings(
                    job["payload"]["revision_ids"]
                )
            elif job["kind"] == "wiki":
                result = WikiService(self.store, self.provider).generate(job["payload"])
            else:
                raise DataError("UNKNOWN_JOB_KIND")
            with self.store.write() as connection:
                result["attempt_history"] = [
                    *job["result"].get("attempt_history", []),
                    {"state": "succeeded", "finished_at": utc_now()},
                ]
                connection.execute(
                    m.jobs.update()
                    .where(m.jobs.c.id == job["id"])
                    .values(state="succeeded", stage="ready", result=result, error_code=None)
                )
        except Exception as exc:
            code = exc.code if isinstance(exc, DataError) else "KNOWLEDGE_JOB_FAILED"
            with self.store.write() as connection:
                connection.execute(
                    m.jobs.update()
                    .where(m.jobs.c.id == job["id"])
                    .values(
                        state="failed",
                        stage="failed",
                        error_code=code,
                        result={
                            "attempt_history": [
                                *job["result"].get("attempt_history", []),
                                {"state": "failed", "error_code": code, "finished_at": utc_now()},
                            ]
                        },
                    )
                )
        return True

    def run(self):
        """
        作用：串行处理队列，异常时保留任务并稍后重试领取
        输入：无
        输出：无
        """
        while not self.stop_event.is_set():
            try:
                worked = self.step()
            except Exception:
                worked = False
            if not worked:
                self.stop_event.wait(0.3)
