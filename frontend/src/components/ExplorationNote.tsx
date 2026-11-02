import { recordOf } from './executionPresentation';

/** 作用：显示服务端冻结的探索身份及声明范围；输入：产物元数据；输出：中文边界说明。 */
export function ExplorationNote({ artifact }: { artifact?: { query_purpose?: unknown; exploration_scope?: unknown } | null }) {
  if (artifact?.query_purpose !== 'exploratory') return null;
  const scope = recordOf(artifact.exploration_scope);
  return <div className="report-note exploration-note" role="note" aria-label="探索结果说明">
    <p>数据探索 · 未绑定已审核项目指标</p>
    <p>以下范围由查询时声明，需结合 SQL 复核。</p>
    {(['population', 'calculation', 'missing_values'] as const).map(key => typeof scope[key] === 'string' && <p key={key}>{({ population: '统计范围', calculation: '计算方式与分母', missing_values: '缺失处理' })[key]}：{scope[key] as string}</p>)}
  </div>;
}
