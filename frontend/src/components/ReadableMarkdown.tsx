import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Citation } from '../api/knowledge';

/** 作用：渲染安全的 Markdown 表格与编号引用；输入：正文和已知引用；输出：阅读视图。 */
export function ReadableMarkdown({ text, citations = [], onCitation }: { text: string; citations?: Citation[]; onCitation?: (id: string) => void }) {
  const ids = citations.map(c => c.chunk_id);
  // 表格末行之后的引用不能被 Markdown 当作多余单元格丢弃；只调整显示，不改原文。
  const readable = text.replace(/(\|)[ \t]*(?=\[chunk_[a-zA-Z0-9_]+\])/g, '$1\n\n').replace(/\[(chunk_[a-zA-Z0-9_]+)\]/g, (_, id: string) => {
    const index = ids.indexOf(id);
    return index < 0 ? '［引用待核对］' : `[${index + 1}](#evidence-${id})`;
  });
  return <div className="readable-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml
    allowedElements={['p', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'li', 'strong', 'em', 'del', 'blockquote', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'hr', 'br', 'sup']}
    urlTransform={url => /^(https?:\/\/|#)/i.test(url) ? url : ''}
    components={{
      a: ({ href, children }) => {
        if (href?.startsWith('#evidence-')) {
          const id = href.slice('#evidence-'.length);
          return ids.includes(id) ? <button className="inline-citation" aria-label={`查看引用 ${ids.indexOf(id) + 1}`} onClick={() => onCitation?.(id)}>{children}</button> : <span>{children}</span>;
        }
        return href ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : <span>{children}</span>;
      },
      table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>,
    }}>{readable}</Markdown></div>;
}
