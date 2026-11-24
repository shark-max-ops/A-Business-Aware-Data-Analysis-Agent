import { useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Input } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';

type Props = {
  question: string;
  onQuestion: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onCompact: () => void;
  onFindings: () => void;
  onData: () => void;
  continuing: boolean;
  active: boolean;
  busy: boolean;
  disabled: boolean;
  canSend: boolean;
  stopping: boolean;
  compactDisabled: string;
  model: string | null;
  meter?: ReactNode;
};

const commandInput = /^\/[a-z\u3400-\u9fff]*$/i;

/** 仅在输入斜杠时展开命令，并沿用现有调查动作。 */
export function AnalysisComposer({
  question, onQuestion, onSend, onStop, onCompact, onFindings, onData,
  continuing, active, busy, disabled, canSend, stopping, compactDisabled, model, meter,
}: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(0);
  const input = useRef<TextAreaRef>(null);
  const menuId = useId();
  const typedCommand = commandInput.test(question.trim());
  const query = question.trim().slice(1).toLowerCase();
  const commands = [
    { name: 'compact', label: '压缩上下文', description: compactDisabled || (active ? '压缩当前轮' : '立即精简当前调查的续查上下文'), action: onCompact, disabled: !!compactDisabled },
    { name: 'findings', label: '调查发现', description: '查看当前调查保存的结果', action: onFindings, disabled: !continuing },
    { name: 'data', label: '工作区数据源', description: '查看数据快照和字段', action: onData, disabled: false },
  ].filter(command => command.name.startsWith(query) || command.label.includes(query));
  const enabled = commands.filter(command => !command.disabled);
  const current = enabled[selected] ?? enabled[0];
  const menuVisible = open && typedCommand && !disabled;

  /** 清除命令文本并执行对应动作，命令不进入问题。 */
  function runCommand(command: typeof commands[number]) {
    setOpen(false);
    onQuestion('');
    input.current?.focus();
    command.action();
  }

  return <div className="composer" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    {menuVisible && <div id={menuId} className="composer-command-menu" role="listbox" aria-label="输入框命令">
      {commands.length ? commands.map(command => (
        <button key={command.name} id={`${menuId}-${command.name}`} type="button" role="option"
          aria-selected={command === current} className="composer-command-option" disabled={command.disabled}
          onMouseDown={event => event.preventDefault()} onClick={() => runCommand(command)}>
          <span className="composer-command-copy"><strong>{command.label}</strong><span>{command.description}</span></span>
          <kbd>/{command.name}</kbd>
        </button>
      )) : <p className="composer-command-empty">没有匹配的命令</p>}
    </div>}
    <Input.TextArea ref={input} aria-label="分析问题" id="question" rows={2} maxLength={4000} value={question}
      placeholder={active ? '分析进行中…' : continuing ? '继续调查…' : '描述你想了解的问题…'}
      disabled={disabled} aria-controls={menuVisible ? menuId : undefined}
      aria-activedescendant={menuVisible && current ? `${menuId}-${current.name}` : undefined}
      onFocus={() => { if (typedCommand) setOpen(true); }}
      onChange={event => {
        const value = event.target.value;
        onQuestion(value);
        setSelected(0);
        setOpen(commandInput.test(value.trim()));
      }}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || !menuVisible) return;
        if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
        if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && enabled.length) {
          event.preventDefault();
          const step = event.key === 'ArrowDown' ? 1 : -1;
          setSelected((selected + step + enabled.length) % enabled.length);
        }
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          if (current) runCommand(current);
        }
      }}
    />
    <div className="composer-bottom">
      <div className="composer-bottom-left">{meter}</div>
      <div className="composer-bottom-right">
        <span className="composer-model" title={model ?? '模型未配置'}>{model ?? '模型未配置'}</span>
        {active ? (
          <Button className="composer-send composer-stop" aria-label="停止分析" title="停止分析" disabled={stopping} onClick={onStop}>
            <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5.5" y="5.5" width="9" height="9" rx="1.7" fill="currentColor" /></svg>
          </Button>
        ) : (
          <Button type="primary" className="composer-send" aria-label="发送问题" title="发送问题"
            loading={busy} disabled={!canSend || typedCommand} onClick={onSend}>
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M10 15V5m-4 4 4-4 4 4" /></svg>
          </Button>
        )}
      </div>
    </div>
  </div>;
}

