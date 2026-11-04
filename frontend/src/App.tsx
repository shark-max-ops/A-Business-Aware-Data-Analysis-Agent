import { lazy, Suspense, useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { Alert, Button, Empty, Spin, Tag } from 'antd';
import { loadHealth } from './api/client';
import type { HealthState } from './api/client';

// 数据管理只在打开抽屉时加载，避免表格组件增加首屏下载量。
const AnalysisPage = lazy(() => import('./components/AnalysisPage').then(module => ({ default: module.AnalysisPage })));
const ReportsPage = lazy(() => import('./components/ReportsPage').then(module => ({ default: module.ReportsPage })));
const ReportDetailPage = lazy(() => import('./components/ReportsPage').then(module => ({ default: module.ReportDetailPage })));
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
      <div className="stage-note"><span>持续调查与外部核查</span><span>查数据与来源 → 保存发现 → 可追溯报告</span></div>
      {state.kind === 'loading' && <div role="status" className="connection-notice"><Spin size="small" /> 正在获取服务状态…</div>}
      {state.kind === 'error' && <Alert type="error" showIcon message="后端连接失败" description={state.message} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
      {state.kind === 'degraded' && <Alert type="warning" showIcon message="服务尚未就绪" description={health?.database !== 'ready' ? '元数据库尚未就绪，请按项目说明完成数据库迁移。' : health?.fts5 !== 'available' ? '当前 SQLite 不支持 FTS5，请检查 Python 环境。' : '知识处理任务未就绪，请重启后端服务。'} action={<Button onClick={() => setAttempt(a => a + 1)}>重新检查</Button>} />}
      <Routes>
        <Route path="/" element={<Navigate to="/analysis" replace />} />
        <Route path="/analysis" element={<Suspense fallback={<p>正在加载分析工作区…</p>}><AnalysisPage /></Suspense>} />
        <Route path="/knowledge" element={<Suspense fallback={<p role="status">正在加载知识管理…</p>}><KnowledgePage /></Suspense>} />
        <Route path="/reports" element={<Suspense fallback={<p>正在加载报告列表…</p>}><ReportsPage /></Suspense>} />
        <Route path="/reports/:reportId/revisions/:revisionId" element={<Suspense fallback={<p>正在加载报告…</p>}><ReportDetailPage /></Suspense>} />
        <Route path="*" element={<div className="empty-panel"><Empty description="页面不存在" /><Link to="/analysis">返回分析页</Link></div>} />
      </Routes>
    </main>
    <footer><span>单用户 · 本地工作区</span><span>固定版本 · 数字可查依据 · 历史报告可回看</span></footer>
  </div>;
}
