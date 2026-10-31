import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Space, Spin, Tag } from 'antd';
import { knowledgeApi } from '../api/knowledge';
import type { GraphResult, GraphNode } from '../api/knowledge';
import { fieldLabels } from './knowledgePresentation';

const columns = ['document', 'concept', 'wiki', 'metric'];
const columnLabels = ['来源资料', '引用主题 / 字段', 'Wiki 解释', '项目指标口径'];
const sourceLabel = (kind: string) => kind === 'official_reference' ? '官方资料' : kind === 'generated_wiki' ? '整理解释' : '项目规则';

/** 作用：用真实引用绘制可点击关系图，不展示 UUID 或推断因果；输入：Wiki 修订和导航；输出：图。 */
export function KnowledgeGraph({ pageId, revisionId, relatedVersion, inspect, openDocument, selectMetric }: {
  pageId: string; revisionId: string; relatedVersion: string; inspect: (id: string) => void;
  openDocument: (doc: string, revision: string) => void; selectMetric: (key: string) => void;
}) {
  const [graph, setGraph] = useState<GraphResult | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [zoom, setZoom] = useState(1);
  const canvas = useRef<HTMLDivElement>(null);
  const [nodeHeights, setNodeHeights] = useState<Record<string, number>>({});
  useEffect(() => {
    let active = true; setGraph(null); setError(''); setSelected(null);
    void knowledgeApi<GraphResult>(`/knowledge/graph?page_id=${encodeURIComponent(pageId)}`).then(value => { if (active) setGraph(value); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [pageId, revisionId, relatedVersion]);
  useLayoutEffect(() => {
    if (!graph || !canvas.current) return;
    const buttons = [...canvas.current.querySelectorAll<HTMLButtonElement>('.graph-node')];
    // 读取未缩放的自然高度，让长标题和状态完整撑开节点；缩放不改变排版尺寸。
    const measure = () => {
      const heights = Object.fromEntries(buttons.map(button => [button.dataset.nodeId!, button.offsetHeight]));
      setNodeHeights(previous => Object.keys(heights).length === Object.keys(previous).length && Object.entries(heights).every(([id, height]) => previous[id] === height) ? previous : heights);
    };
    measure();
    const observer = new ResizeObserver(measure);
    buttons.forEach(button => observer.observe(button));
    return () => observer.disconnect();
  }, [graph]);
  if (error) return <Alert type="error" message="关系图读取失败" description={error} />;
  if (!graph) return <Spin tip="读取真实引用关系…"><div style={{ height: 160 }} /></Spin>;
  if (!graph.nodes.length) return <Empty description="还没有可展示的引用关系" />;
  const groups = columns.map(kind => graph.nodes.filter(n => n.kind === kind));
  const nodeHeight = (id: string) => nodeHeights[id] ?? 84;
  const columnHeights = groups.map(group => group.reduce((total, node) => total + nodeHeight(node.id), 0) + Math.max(0, group.length - 1) * 20);
  const height = Math.max(330, Math.max(...columnHeights) + 80);
  const positions = new Map<string, { x: number; y: number }>();
  groups.forEach((group, column) => {
    let y = 50 + (height - 80 - columnHeights[column]) / 2;
    group.forEach(node => { positions.set(node.id, { x: 16 + column * 238, y }); y += nodeHeight(node.id) + 20; });
  });
  const linked = (source: string, target: string) => !selected || selected.id === source || selected.id === target;
  return <section className="knowledge-graph" aria-label="知识关系导航">
    <Space wrap><strong>资料如何支持 Wiki 和指标</strong><Tag color="green">官方资料</Tag><Tag color="gold">项目规则</Tag><Tag color="blue">Wiki</Tag>
      <Button size="small" aria-label="缩小关系图" onClick={() => setZoom(v => Math.max(0.65, v - 0.15))}>−</Button><Button size="small" aria-label="放大关系图" onClick={() => setZoom(v => Math.min(1.5, v + 0.15))}>＋</Button><Button size="small" onClick={() => { setZoom(1); setSelected(null); }}>重置视图</Button>
    </Space>
    <p className="muted">点击节点查看关联；点击下方按钮打开原文或指标。图中包括草稿，连线表示引用与来源，不表示因果。</p>
    <div ref={canvas} className="knowledge-graph-canvas"><svg role="group" aria-label="知识引用关系图" width={970 * zoom} height={height * zoom} viewBox={`0 0 970 ${height}`}>
      <defs><marker id="knowledge-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#8aa59a" /></marker></defs>
      {columnLabels.map((label, i) => <text key={label} x={16 + i * 238} y={23} fill="#536e61" fontSize={13}>{label}</text>)}
      {graph.edges.map(edge => {
        const from = positions.get(edge.source); const to = positions.get(edge.target);
        if (!from || !to) return null;
        const x = from.x + 208, y = from.y + nodeHeight(edge.source) / 2, targetY = to.y + nodeHeight(edge.target) / 2;
        return <path key={`${edge.source}-${edge.target}`} d={`M${x},${y} C${x + 36},${y} ${to.x - 36},${targetY} ${to.x},${targetY}`} fill="none" stroke={linked(edge.source, edge.target) ? '#719f89' : '#d9e3de'} strokeWidth={selected && linked(edge.source, edge.target) ? 2.5 : 1.2} opacity={linked(edge.source, edge.target) ? 1 : 0.35} markerEnd="url(#knowledge-arrow)"><title>{edge.label}</title></path>;
      })}
      {graph.nodes.map(node => { const pos = positions.get(node.id)!; return <foreignObject key={node.id} x={pos.x} y={pos.y} width="212" height={nodeHeight(node.id) + 6}><button data-node-id={node.id} className={`graph-node graph-${node.kind} ${node.source_kind} ${selected?.id === node.id ? 'selected' : ''}`} aria-label={`关系节点：${fieldLabels[node.label] ?? node.label}`} onClick={() => setSelected(node)}><strong>{fieldLabels[node.label] ?? node.label}</strong><span>{sourceLabel(node.source_kind)}{node.status ? ` · ${node.status === 'draft' ? '待审核' : node.status === 'approved' ? '已审核' : node.status === 'published' ? '已发布' : node.status}` : ''}</span></button></foreignObject>; })}
    </svg></div>
    {selected && <div className="graph-node-detail"><strong>{fieldLabels[selected.label] ?? selected.label}</strong><span>{sourceLabel(selected.source_kind)}</span>
      {selected.chunk_id && <Button onClick={() => inspect(selected.chunk_id!)}>查看该主题原文</Button>}
      {selected.kind === 'document' && selected.document_id && selected.revision_id && <Button onClick={() => openDocument(selected.document_id!, selected.revision_id!)}>打开来源资料</Button>}
      {selected.metric_key && <Button onClick={() => selectMetric(selected.metric_key!)}>查看这个指标怎么算</Button>}
    </div>}
  </section>;
}
