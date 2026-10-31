import { lazy, Suspense, useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom';
import { Alert, Button, Empty, Spin, Tag } from 'antd';
import { loadHealth } from './api/client';
import type { HealthState } from './api/client';

// 数据管理只在打开抽屉时加载，避免表格组件增加首屏下载量。
const AnalysisPage = lazy(() => import('./components/AnalysisPage').then(module => ({ default: module.AnalysisPage })));
const KnowledgePage = lazy(() => import('./components/KnowledgePage').then(module => ({ default: module.KnowledgePage })));

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
      <Link to="/analysis" className="brand"><span className="brand-mark">A</span><span className="brand-title">A Business-Aware Data Analysis Agent<small>业务口径驱动的数据分析</small></span></Link>
      <nav aria-label="主导航"><NavLink to="/knowledge">知识管理</NavLink><NavLink to="/analysis">分析</NavLink><NavLink to="/reports">报告</NavLink></nav>
      <div className="connection"><span className={`status-dot ${state.kind === 'ready' ? 'online' : ''}`} />{state.kind === 'loading' ? '正在连接服务' : state.kind === 'error' ? '后端未连接' : state.kind === 'degraded' ? '服务需要处理' : '本地服务已连接'}<Tag>{health?.llm === 'configured_unverified' ? '模型配置已填写' : health ? '模型未配置' : '模型状态未知'}</Tag></div>
    </header>
    <main>
      <div className="stage-note"><span>分析 Agent 与工具进度 · 阶段 3</span><span>知识与指标 → 受限查询 → 可追溯报告</span></div>
      {state.kind === 'loading' && <div role="status" className="connection-notice"><Spin size="small" /> 正在获取服务状态…</div>}
      {state.kind === 'error' && <Alert type="error" showIcon message="后端连接失败" description={state.message} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
      {state.kind === 'degraded' && <Alert type="warning" showIcon message="服务尚未就绪" description={health?.database !== 'ready' ? '元数据库尚未就绪，请按项目说明完成数据库迁移。' : health?.fts5 !== 'available' ? '当前 SQLite 不支持 FTS5，请检查 Python 环境。' : '知识处理任务未就绪，请重启后端服务。'} action={<Button onClick={() => setAttempt(a => a + 1)}>重新检查</Button>} />}
      <Routes>
        <Route path="/" element={<Navigate to="/analysis" replace />} />
        <Route path="/analysis" element={<Suspense fallback={<p>正在加载分析工作区…</p>}><AnalysisPage /></Suspense>} />
        <Route path="/knowledge" element={<Suspense fallback={<p role="status">正在加载知识管理…</p>}><KnowledgePage /></Suspense>} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/reports/:reportId/revisions/:revisionId" element={<ReportDetail />} />
        <Route path="*" element={<div className="empty-panel"><Empty description="页面不存在" /><Link to="/analysis">返回分析页</Link></div>} />
      </Routes>
    </main>
    <footer><span>单用户 · 本地工作区</span><span>自然语言分析已接入；完整可追溯报告将在阶段 4 开放。</span></footer>
  </div>;
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
