import { useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

type Props = { title: string; selected: boolean; disabled: boolean; onClick: () => void };

/** 作用：单行显示会话标题并按实际溢出距离滚动；输入：标题与选择状态；输出：会话按钮。 */
export function ConversationItem({ title, selected, disabled, onClick }: Props) {
  const viewport = useRef<HTMLSpanElement>(null);
  const text = useRef<HTMLSpanElement>(null);
  const [distance, setDistance] = useState(0);
  useLayoutEffect(() => {
    let alive = true;
    /** 作用：测量完整文字与可见宽度；输入：无；输出：更新滚动距离。 */
    const measure = () => {
      if (alive && viewport.current && text.current) {
        setDistance(Math.max(0, text.current.getBoundingClientRect().width - viewport.current.clientWidth));
      }
    };
    const observer = new ResizeObserver(measure);
    if (viewport.current) observer.observe(viewport.current);
    measure();
    void document.fonts.ready.then(measure);
    return () => { alive = false; observer.disconnect(); };
  }, [title]);
  const style = { '--title-travel': `-${distance}px`, '--title-duration': `${Math.max(2, distance / 40)}s` } as CSSProperties;
  return <button type="button" className={`conversation-item${selected ? ' selected' : ''}`} aria-current={selected ? 'page' : undefined} aria-label={title} disabled={disabled} onClick={onClick}>
    <span ref={viewport} className={`conversation-title${distance > 1 ? ' overflowing' : ''}`} style={style}><span ref={text} className="conversation-title-text">{title}</span></span>
  </button>;
}
