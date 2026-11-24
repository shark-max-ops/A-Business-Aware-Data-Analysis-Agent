import { SharkMark } from './components/SharkMark';
import { lazy, Suspense, useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Alert, Button, Empty, Spin } from 'antd';
import { loadHealth } from './api/client';
import type { HealthState } from './api/client';

const AnalysisPage = lazy(() => import('./components/AnalysisPage').then(module => ({ default: module.AnalysisPage })));
const ReportsPage = lazy(() => import('./components/ReportsPage').then(module => ({ default: module.ReportsPage })));
const ReportDetailPage = lazy(() => import('./components/ReportsPage').then(module => ({ default: module.ReportDetailPage })));
const KnowledgePage = lazy(() => import('./components/KnowledgePage').then(module => ({ default: module.KnowledgePage })));

/** 作用：在统一侧栏与内容画布中展示真实工作区；输入：无；输出：应用页面。 */
export function App() {
  const [state, setState] = useState<HealthState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: 'loading' });
    void loadHealth(controller.signal).then(result => { if (!controller.signal.aborted) setState(result); }).catch(() => {});
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => { setSidebarOpen(false); }, [location.pathname]);
  const health = state.kind === 'ready' || state.kind === 'degraded' ? state.health : null;
  const analysis = location.pathname === '/analysis' || location.pathname === '/';
  return <div className={`app-shell${sidebarOpen ? ' sidebar-open' : ''}`}>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label="收起侧栏遮罩" onClick={() => setSidebarOpen(false)} />}
    <aside className="workspace-sidebar" aria-label="工作区侧栏" onClick={event => {
      if ((event.target as HTMLElement).closest('.conversation-item, .new-conversation')) setSidebarOpen(false);
    }}>
      <Button className="sidebar-close" type="text" aria-label="关闭导航" onClick={() => setSidebarOpen(false)}><svg className="ui-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg></Button>
      <Link to="/analysis" className="brand"><SharkMark className="brand-mark" /><span className="brand-copy"><strong className="brand-name">Caliber</strong><small>Business-aware analysis</small></span></Link>
      <nav aria-label="主导航">
        <NavLink to="/analysis"><svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2.5 12.5h11M4.5 9.5l2.5-3 2.5 2 3-4.5" /></svg>分析</NavLink>
        <NavLink to="/knowledge"><svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 3.5A1.5 1.5 0 0 1 4.5 2H13v11.5H4.5A1.5 1.5 0 0 1 3 12V3.5Zm0 8.5A1.5 1.5 0 0 1 4.5 10.5H13" /></svg>知识管理</NavLink>
        <NavLink to="/reports"><svg className="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="2" width="10" height="12" rx="1.5" /><path d="M5.5 6h5M5.5 9h5M5.5 11.5h3" /></svg>报告</NavLink>
      </nav>
      <div id="workspace-sessions" className="workspace-sessions" />
      {!analysis && <div className="sidebar-note">从资料出发，<br />让每个发现都有依据。</div>}
      <div className="sidebar-footer">
        <div className="connection"><span className={`status-dot ${state.kind === 'ready' ? 'online' : ''}`} />{state.kind === 'loading' ? '正在连接服务' : state.kind === 'error' ? '后端未连接' : state.kind === 'degraded' ? '服务需要处理' : '本地服务已连接'}</div>
        <span className="model-status">{health?.llm === 'configured_unverified' ? '模型配置已填写' : health ? '模型未配置' : '模型状态未知'}</span>
        <small>单用户 · 本地工作区</small>
      </div>
    </aside>
    <div className="workspace-body">
      <header className="mobile-workspace-header"><Button type="text" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><svg className="ui-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" /></svg></Button><span className="mobile-brand-name">Caliber</span><span className={`status-dot ${state.kind === 'ready' ? 'online' : ''}`} /></header>
      <main className={`workspace-content ${analysis ? 'route-analysis' : 'route-library'}`}>
        <div className="service-notices">
          {state.kind === 'loading' && <div role="status" className="connection-notice"><Spin size="small" /> 正在获取服务状态…</div>}
          {state.kind === 'error' && <Alert type="error" showIcon message="后端连接失败" description={state.message} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
          {state.kind === 'degraded' && <Alert type="warning" showIcon message="服务尚未就绪" description={health?.database !== 'ready' ? '请按项目说明完成数据库迁移。' : health?.fts5 !== 'available' ? '当前 SQLite 不支持 FTS5，请检查 Python 环境。' : '知识处理服务未就绪，请重启后端。'} action={<Button onClick={() => setAttempt(a => a + 1)}>重新检查</Button>} />}
        </div>
        <Routes>
          <Route path="/" element={<Navigate to="/analysis" replace />} />
          <Route path="/analysis" element={<Suspense fallback={<p role="status">正在加载分析工作区…</p>}><AnalysisPage /></Suspense>} />
          <Route path="/knowledge" element={<Suspense fallback={<p role="status">正在加载知识管理…</p>}><KnowledgePage /></Suspense>} />
          <Route path="/reports" element={<Suspense fallback={<p role="status">正在加载报告…</p>}><ReportsPage /></Suspense>} />
          <Route path="/reports/:reportId/revisions/:revisionId" element={<Suspense fallback={<p role="status">正在加载报告…</p>}><ReportDetailPage /></Suspense>} />
          <Route path="*" element={<div className="empty-panel"><Empty description="页面不存在" /><Link to="/analysis">返回分析页</Link></div>} />
        </Routes>
      </main>
    </div>
  </div>;
}
