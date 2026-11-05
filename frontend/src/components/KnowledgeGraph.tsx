import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Space, Spin, Tag } from 'antd';
import { knowledgeApi } from '../api/knowledge';
import type { GraphResult, GraphNode } from '../api/knowledge';
import { fieldLabels } from './knowledgePresentation';

const kinds: Record<string, string> = { document: '参考文档', concept: '引用主题', wiki: 'Wiki', metric: '计算规则' };
const label = (node: GraphNode) => fieldLabels[node.label] ?? node.label;

/** 作用：以圆形节点显示真实引用，选中后在图下导航；输入：Wiki 修订与打开回调；输出：关系地图。 */
export function KnowledgeGraph({ pageId, revisionId, relatedVersion, inspect, openDocument, selectMetric, readWiki }: {
  pageId: string; revisionId: string; relatedVersion: string; inspect: (id: string) => void;
  openDocument: (doc: string, revision: string) => void; selectMetric: (key: string) => void; readWiki?: () => void;
}) {
  const [graph, setGraph] = useState<GraphResult | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [zoom, setZoom] = useState(1);
  const canvas = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(0);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setCanvasWidth(element.clientWidth));
    observer.observe(element);
    return () => observer.disconnect();
  }, [graph]);
  useEffect(() => {
    let active = true; setGraph(null); setError(''); setSelected(null);
    void knowledgeApi<GraphResult>(`/knowledge/graph?page_id=${encodeURIComponent(pageId)}`).then(value => { if (active) setGraph(value); }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [pageId, revisionId, relatedVersion]);
  if (error) return <Alert type="error" message="关系图读取失败" description={error} />;
  if (!graph) return <Spin tip="读取引用关系…"><div style={{ height: 200 }} /></Spin>;
  if (!graph.nodes.length) return <Empty description="添加资料和引用后，这里会呈现它们的联系" />;
  const centerNodes = graph.nodes.filter(node => node.kind === 'wiki');
  const outer = graph.nodes.filter(node => node.kind !== 'wiki');
  const radiusX = Math.max(300, outer.length * 20);
  const radiusY = Math.max(180, outer.length * 10);
  const width = radiusX * 2 + 168;
  const height = radiusY * 2 + 168;
  const scale = Math.min(1, (canvasWidth || width) / width) * zoom;
  const centerX = width / 2;
  const centerY = height / 2;
  const positions = new Map<string, { x: number; y: number }>();
  outer.forEach((node, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / Math.max(outer.length, 1);
    positions.set(node.id, { x: centerX + radiusX * Math.cos(angle), y: centerY + radiusY * Math.sin(angle) });
  });
  centerNodes.forEach((node, index) => positions.set(node.id, { x: centerX + (index - (centerNodes.length - 1) / 2) * 118, y: centerY }));
  const connections = new Set(selected ? graph.edges.filter(edge => edge.source === selected.id || edge.target === selected.id).flatMap(edge => [edge.source, edge.target]) : []);
  const related = selected ? graph.nodes.filter(node => node.id !== selected.id && connections.has(node.id)) : [];

  /** 作用：打开节点实际绑定的原文或规则；输入：真实节点；输出：调用已有版本化读取。 */
  const openNode = (node: GraphNode) => {
    if (node.kind === 'document' && node.document_id && node.revision_id) openDocument(node.document_id, node.revision_id);
    else if (node.chunk_id) inspect(node.chunk_id);
    else if (node.metric_key) selectMetric(node.metric_key);
    else if (node.kind === 'wiki') readWiki?.();
  };

  return <section className="knowledge-graph" aria-label="知识关系导航">
    <div className="graph-toolbar"><div className="graph-legend">{Object.entries(kinds).map(([kind, text]) => <span key={kind}><i className={`legend-${kind}`} />{text}</span>)}</div><Space><Button size="small" aria-label="缩小关系图" onClick={() => setZoom(value => Math.max(0.65, value - 0.15))}>−</Button><span className="graph-zoom">{Math.round(zoom * 100)}%</span><Button size="small" aria-label="放大关系图" onClick={() => setZoom(value => Math.min(1.5, value + 0.15))}>＋</Button><Button size="small" onClick={() => { setZoom(1); setSelected(null); }}>重置视图</Button></Space></div>
    <div ref={canvas} className="knowledge-graph-canvas"><svg role="group" aria-label="知识引用关系图" width={width * scale} height={height * scale} viewBox={`0 0 ${width} ${height}`}>
      {graph.edges.map(edge => {
        const from = positions.get(edge.source); const to = positions.get(edge.target);
        if (!from || !to) return null;
        const highlighted = !selected || edge.source === selected.id || edge.target === selected.id;
        return <path key={`${edge.source}-${edge.target}`} d={`M${from.x},${from.y} Q${centerX},${centerY} ${to.x},${to.y}`} fill="none" className={`graph-edge${highlighted ? ' connected' : ''}`}><title>{edge.label}</title></path>;
      })}
      {graph.nodes.map(node => {
        const point = positions.get(node.id)!;
        const characters = Array.from(label(node));
        const lines = [characters.slice(0, 7).join(''), characters.slice(7, 14).join(''), characters.slice(14, 20).join('') + (characters.length > 20 ? '…' : '')].filter(Boolean);
        return <g key={node.id} transform={`translate(${point.x} ${point.y})`} role="button" tabIndex={0} aria-label={`关系节点：${label(node)}`} aria-pressed={selected?.id === node.id} data-node-id={node.id} className={`graph-node graph-${node.kind}${selected?.id === node.id ? ' selected' : ''}${selected && !connections.has(node.id) && selected.id !== node.id ? ' dimmed' : ''}`} onClick={() => setSelected(node)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(node); } }}>
          <title>{label(node)}</title><g className="graph-node-shape"><circle r={node.kind === 'wiki' ? 50 : 46} />{lines.map((line, index) => <text key={index} textAnchor="middle" y={(index - (lines.length - 1) / 2) * 16 + 4}>{line}</text>)}</g>
          <text className="graph-node-caption" textAnchor="middle" y={69}>{kinds[node.kind] ?? '资料'}{node.status === 'draft' ? ' · 草稿' : node.status === 'approved' ? ' · 已审核' : ''}</text>
        </g>;
      })}
    </svg></div>
    {selected ? <article className="graph-node-detail" aria-live="polite"><div className="graph-detail-heading"><span className={`document-icon legend-${selected.kind}`} aria-hidden="true">{selected.kind === 'metric' ? 'ƒ' : '▤'}</span><div><Tag>{kinds[selected.kind] ?? '资料'}</Tag><h3>{label(selected)}</h3></div><Button type="primary" onClick={() => openNode(selected)}>{selected.kind === 'metric' ? '查看规则文档' : selected.kind === 'wiki' ? '阅读 Wiki 正文' : '查看原文'} ↗</Button></div>
      {related.length > 0 && <div className="graph-related"><span>关联文档与主题</span>{related.map(node => <Button key={node.id} onClick={() => { setSelected(node); }}>{label(node)}</Button>)}</div>}
      <p className="muted">连线来自已保存的引用，不表示因果关系。草稿不会自动成为正式规则。</p>
    </article> : <div className="graph-selection-hint"><span aria-hidden="true">↖</span> 悬停探索，点击一个节点，在这里查看关联文档。</div>}
  </section>;
}
