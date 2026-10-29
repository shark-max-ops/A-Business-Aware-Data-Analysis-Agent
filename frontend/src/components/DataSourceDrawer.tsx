import { useEffect, useState } from 'react';
import { Alert, Button, Descriptions, Drawer, Empty, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import { requestData } from '../api/data';
import type { LoadState, Schema, Snapshot, Source } from '../api/data';

/** 作用：查看真实快照、质量和表结构；输入：打开状态与关闭回调；输出：数据源抽屉。 */
export function DataSourceDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [sources, setSources] = useState<LoadState<Source[]>>({ kind: 'loading' });
  const [snapshots, setSnapshots] = useState<LoadState<Snapshot[]>>({ kind: 'loading' });
  const [schema, setSchema] = useState<LoadState<Schema>>({ kind: 'loading' });
  const [sourceId, setSourceId] = useState<string>();
  const [snapshotId, setSnapshotId] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const [activation, setActivation] = useState<'idle' | 'saving'>('idle');
  const [actionError, setActionError] = useState('');

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setSources({ kind: 'loading' });
    void requestData<{ items: Source[] }>('/sources', { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setSources({ kind: 'ready', data: result.items });
      setSourceId(current => result.items.some(s => s.id === current) ? current
        : result.items.find(s => s.source_kind === 'official_bts' && s.current_snapshot_id)?.id
          ?? result.items.find(s => s.current_snapshot_id)?.id ?? result.items[0]?.id);
    }).catch(error => { if (!controller.signal.aborted) setSources({ kind: 'error', message: String(error.message) }); });
    return () => controller.abort();
  }, [open, attempt]);

  useEffect(() => {
    if (!open || !sourceId) return;
    const controller = new AbortController();
    setSnapshots({ kind: 'loading' });
    setSnapshotId(undefined);
    setActionError('');
    void requestData<{ items: Snapshot[] }>(`/sources/${sourceId}/snapshots`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setSnapshots({ kind: 'ready', data: result.items });
      setSnapshotId(result.items[0]?.id);
    }).catch(error => { if (!controller.signal.aborted) setSnapshots({ kind: 'error', message: String(error.message) }); });
    return () => controller.abort();
  }, [open, sourceId, attempt]);

  useEffect(() => {
    if (!open || !sourceId || !snapshotId) return;
    const controller = new AbortController();
    setSchema({ kind: 'loading' });
    void requestData<Schema>(`/sources/${sourceId}/schema?snapshot_id=${encodeURIComponent(snapshotId)}`, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setSchema({ kind: 'ready', data: result });
    }).catch(error => { if (!controller.signal.aborted) setSchema({ kind: 'error', message: String(error.message) }); });
    return () => controller.abort();
  }, [open, sourceId, snapshotId]);

  const selected = snapshots.kind === 'ready' ? snapshots.data.find(s => s.id === snapshotId) : undefined;
  const source = sources.kind === 'ready' ? sources.data.find(s => s.id === sourceId) : undefined;

  /** 作用：明确提交默认快照切换并显示实际结果；输入：当前选择；输出：更新状态。 */
  async function activate() {
    if (!sourceId || !snapshotId) return;
    setActivation('saving');
    setActionError('');
    try {
      const updated = await requestData<Source>(`/sources/${sourceId}/activate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: snapshotId }),
      });
      setSources(current => current.kind === 'ready' ? { kind: 'ready', data: current.data.map(s => s.id === updated.id ? updated : s) } : current);
    } catch (error) { setActionError(error instanceof Error ? error.message : '设置默认快照失败'); }
    finally { setActivation('idle'); }
  }

  return <Drawer title="数据源与固定快照" open={open} onClose={onClose} width={800}>
    <p>通过项目导入命令登记 CSV 后，可在这里查看快照。选择预览不会改变默认版本。</p>
    <Button onClick={() => setAttempt(a => a + 1)}>刷新数据源</Button>
    {sources.kind === 'loading' && <p role="status"><Spin size="small" /> 正在读取数据源…</p>}
    {sources.kind === 'error' && <Alert type="error" message="数据源读取失败" description={sources.message} />}
    {sources.kind === 'ready' && sources.data.length === 0 && <Empty description="尚未导入数据" />}
    {sources.kind === 'ready' && sources.data.length > 0 && <Space direction="vertical" size="middle" style={{ width: '100%', marginTop: 20 }}>
      <label>数据源<Select aria-label="数据源" value={sourceId} onChange={setSourceId} style={{ width: '100%' }} options={sources.data.map(s => ({ value: s.id, label: s.name }))} /></label>
      {source && <Tag color={source.source_kind === 'synthetic' ? 'orange' : 'blue'}>{source.source_kind === 'synthetic' ? '人工合成测试数据' : '用户登记的 BTS Reporting 来源'}</Tag>}
      <p>当前默认快照：<code style={{ overflowWrap: 'anywhere' }}>{source?.current_snapshot_id || '尚未设置'}</code></p>
      {snapshots.kind === 'loading' && <Spin size="small" />}
      {snapshots.kind === 'error' && <Alert type="error" message="快照读取失败" description={snapshots.message} />}
      {snapshots.kind === 'ready' && snapshots.data.length === 0 && <Empty description="这个来源尚无快照" />}
      {snapshots.kind === 'ready' && snapshots.data.length > 0 && <label>预览快照<Select aria-label="预览快照" value={snapshotId} onChange={setSnapshotId} style={{ width: '100%' }} options={snapshots.data.map(s => ({ value: s.id, label: `${s.period} · ${s.row_count.toLocaleString()} 行 · ${new Date(s.created_at).toLocaleString()} · ${s.id.slice(-8)}` }))} /></label>}
      {actionError && <Alert type="error" message={actionError} />}
      {selected && <>
        <Button type="primary" loading={activation === 'saving'} disabled={source?.current_snapshot_id === snapshotId} onClick={() => void activate()}>{source?.current_snapshot_id === snapshotId ? '已是默认快照' : '设为默认快照'}</Button>
        <Descriptions bordered size="small" column={1} items={[
          { key: 'period', label: '月份', children: selected.period },
          { key: 'rows', label: '公开记录数', children: selected.row_count.toLocaleString() },
          { key: 'id', label: '快照 ID', children: <code style={{ overflowWrap: 'anywhere' }}>{selected.id}</code> },
          { key: 'hash', label: '原文件 SHA-256', children: <code style={{ overflowWrap: 'anywhere' }}>{selected.file_hash}</code> },
        ]} />
        <Tabs items={[
          { key: 'schema', label: '可查询表结构', children: <>
            {schema.kind === 'loading' && <Spin size="small" />}
            {schema.kind === 'error' && <Alert type="error" message="表结构读取失败" description={schema.message} />}
            {schema.kind === 'ready' && schema.data.tables.map(table => <section key={table.name} style={{ marginBottom: 28 }}><h3>{table.name}</h3><p>关联键：{(table.join_keys ?? []).join('、')}</p><Table size="small" pagination={false} rowKey="name" scroll={{ x: 550 }} dataSource={table.columns} columns={[{ title: '字段', dataIndex: 'name' }, { title: '类型', dataIndex: 'type' }, { title: '说明', dataIndex: 'description' }]} /><details><summary>导入时真实样本（最多 5 行）</summary><pre style={{ overflowX: 'auto' }}>{JSON.stringify(table.sample, null, 2)}</pre></details></section>)}
          </> },
          { key: 'quality', label: '质量报告', children: <pre style={{ overflowX: 'auto' }}>{JSON.stringify(selected.quality_report, null, 2)}</pre> },
          { key: 'manifest', label: '来源清单', children: <pre style={{ overflowX: 'auto' }}>{JSON.stringify(selected.manifest, null, 2)}</pre> },
        ]} />
      </>}
    </Space>}
  </Drawer>;
}
