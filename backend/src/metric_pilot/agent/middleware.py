"""四个固定观测 hook；安全检查继续由 Runtime 和执行器强制执行。"""


class RuntimeObserver:
    """
    作用：组合进度及审计
    输入：运行仓储
    输出：四个观测入口
    """

    def __init__(self, store):
        """
        作用：绑定仓储
        输入：仓储
        输出：无
        """
        self.store = store

    def before_model(self, run_id, turn):
        """
        作用：记录真实模型请求进度
        输入：运行及回合
        输出：无
        """
        self.store.emit(
            run_id, "run.progress", {"summary": "模型正在选择下一步工具或提交结果", "turn": turn}
        )

    def before_tool(self, run_id, call):
        """
        作用：记录工具开始
        输入：运行及调用
        输出：执行 ID
        """
        return self.store.tool_start(run_id, call)

    def after_tool(self, run_id, execution, name, result, duration_ms):
        """
        作用：记录实际反馈
        输入：工具结果
        输出：无
        """
        self.store.tool_finish(run_id, execution, name, result, duration_ms)

    def on_finish(
        self,
        run_id,
        status,
        result=None,
        error_code=None,
        usage=None,
        conversation_title=None,
        **report,
    ):
        """
        作用：持久化终态
        输入：实际结果
        输出：无
        """
        self.store.finish(run_id, status, result, error_code, usage, conversation_title, **report)
