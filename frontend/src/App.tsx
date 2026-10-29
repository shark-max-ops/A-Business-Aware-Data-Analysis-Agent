import { useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom';
import { Alert, Button, Drawer, Empty, Input, Spin, Tabs, Tag } from 'antd';
import { loadHealth } from './api/client';
import type { HealthState } from './api/client';

/** 作用：展示导航和服务状态；输入：无；输出：应用页面。 */
export function App() {
  const [state, setState] = useState<HealthState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: 'loading' });
    void loadHealth(controller.signal).then(result => { if (!controller.signal.aborted) setState(result); }).catch(() => {});
    return () => controller.abort();
  }, [attempt]);
  const health = state.kind === 'ready' || state.kind === 'degraded' ? state.health : null;
  return <div className="app-shell">
    <header className="topbar">
      <Link to="/analysis" className="brand"><span className="brand-mark">M</span><span>MetricPilot<small>业务口径驱动的数据分析</small></span></Link>
      <nav aria-label="主导航"><NavLink to="/knowledge">知识管理</NavLink><NavLink to="/analysis">分析</NavLink><NavLink to="/reports">报告</NavLink></nav>
      <div className="connection"><span className={`status-dot ${state.kind === 'ready' ? 'online' : ''}`} />{state.kind === 'loading' ? '正在连接服务' : state.kind === 'error' ? '后端未连接' : state.kind === 'degraded' ? '服务需要处理' : '本地服务已连接'}<Tag>{health?.llm === 'configured_unverified' ? '模型配置待验证' : health ? '模型未配置' : '模型状态未知'}</Tag></div>
    </header>
    <main>
      <div className="stage-note"><span>工程初始化 · 阶段 0</span><span>知识与指标 → 受限查询 → 可追溯报告</span></div>
      {state.kind === 'loading' && <div role="status" className="connection-notice"><Spin size="small" /> 正在获取服务状态…</div>}
      {state.kind === 'error' && <Alert type="error" showIcon message="后端连接失败" description={state.message} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
      {state.kind === 'degraded' && <Alert type="warning" showIcon message="服务尚未就绪" description={health?.database !== 'ready' ? '元数据库尚未就绪，请按项目说明完成数据库迁移。' : '当前 SQLite 不支持 FTS5，请检查 Python 环境。'} action={<Button onClick={() => setAttempt(a => a + 1)}>重新检查</Button>} />}
      <Routes>
        <Route path="/" element={<Navigate to="/analysis" replace />} />
        <Route path="/analysis" element={<Analysis />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/reports/:reportId/revisions/:revisionId" element={<ReportDetail />} />
        <Route path="*" element={<div className="empty-panel"><Empty description="页面不存在" /><Link to="/analysis">返回分析页</Link></div>} />
      </Routes>
    </main>
    <footer><span>单用户 · 本地工作区</span><span>阶段 0 已建立页面框架，业务功能将按阶段开放。</span></footer>
  </div>;
}

/** 作用：展示分析入口和数据源抽屉；输入：无；输出：分析页面壳。 */
function Analysis() {
  const [open, setOpen] = useState(false);
  return <><div className="page-heading"><div><p className="eyebrow">ANALYSIS WORKSPACE</p><h1>从业务问题开始分析</h1><p>先明确指标口径，再用真实查询建立证据。</p></div><Button onClick={() => setOpen(true)}>查看数据源</Button></div>
    <div className="analysis-grid">
      <aside className="panel"><h2>分析会话</h2><div className="aside-empty">会话功能尚未开放</div></aside>
      <section className="panel question-panel"><div className="panel-top"><h2>分析工作区</h2><Tag>尚未开放</Tag></div><div className="analysis-empty"><span className="empty-symbol">↗</span><h3>准备好数据与规则后，即可开始</h3><p>数据导入将在阶段 1 开放，知识与指标将在阶段 2 开放，模型分析将在阶段 3 接入。</p><div className="prerequisites"><span>① 数据快照</span><span>② 已审核指标</span><span>③ 模型配置</span></div></div><div className="composer"><label htmlFor="question">分析问题</label><Input.TextArea id="question" disabled rows={2} placeholder="分析功能尚未开放" /><div className="composer-bottom"><span>当前尚不能提交分析任务</span><Button type="primary" disabled>发送问题</Button></div></div></section>
      <aside className="panel"><h2>执行进度</h2><p className="aside-empty">分析运行后，这里会展示真实工具调用、结果与错误。</p></aside>
    </div>
    <Drawer title="数据源" open={open} onClose={() => setOpen(false)}><Empty description="数据导入尚未开放" /><p>阶段 1 将支持 BTS Reporting Carrier CSV 导入、数据快照和表结构查看。</p></Drawer></>;
}

/** 作用：展示知识管理未开放状态；输入：无；输出：知识页面壳。 */
function Knowledge() {
  return <><div className="page-heading"><div><p className="eyebrow">KNOWLEDGE & METRICS</p><h1>为每个指标建立依据</h1><p>官方资料、项目规则与生成 Wiki 分别管理，保留来源和版本。</p></div></div><section className="panel knowledge-panel"><Tabs items={[
    { key: 'documents', label: '原始文档', children: <EmptyArea text="文档导入尚未开放" note="阶段 2 将支持 Markdown / TXT 导入、版本保存和来源定位。" /> },
    { key: 'wiki', label: 'Wiki', children: <EmptyArea text="Wiki 管理尚未开放" note="阶段 2 将支持生成草稿、人工编辑、发布和历史版本查看。" /> },
    { key: 'metrics', label: '指标定义', children: <EmptyArea text="指标管理尚未开放" note="阶段 2 将建立公式、分母、过滤范围与适用期的结构化定义。" /> },
  ]} /></section></>;
}

/** 作用：展示报告入口；输入：无；输出：报告页面壳。 */
function Reports() {
  return <><div className="page-heading"><div><p className="eyebrow">TRACEABLE REPORTS</p><h1>让每个数字都有出处</h1><p>报告将固定数据与规则版本，数字可追溯到实际查询。</p></div></div><section className="panel"><EmptyArea text="报告功能尚未开放" note="阶段 4 将支持 KPI、图表、表格和证据查看。当前尚不能生成报告。" /></section></>;
}

/** 作用：显示报告详情尚未开放；输入：路由参数；输出：详情占位页。 */
function ReportDetail() {
  const { reportId, revisionId } = useParams();
  return <section className="panel detail-panel"><h1>报告详情尚未开放</h1><p>当前阶段无法加载报告 {reportId} 的修订 {revisionId}。</p><Link to="/reports">返回报告列表</Link></section>;
}

/** 作用：统一空态布局；输入：说明文本；输出：空态组件。 */
function EmptyArea({ text, note }: { text: string; note: string }) {
  return <div className="empty-panel"><Empty description={text} /><p>{note}</p></div>;
}
