import React from 'react';

/**
 * 页签级错误边界。
 *
 * 动机：验收时 `#notes` 出现过一次"整页空白（root 0 子节点）"、3 次冷加载未复现。
 * React 18 里任何渲染期异常若没有边界接住，会把整棵树卸载 —— 表现就是 root 被清空的白屏，
 * 而"白屏"恰好是最难自证的一种故障。加了这层边界后，即使某个页签的渲染真的抛错：
 * 1. 只有该页签退化成一张可读的错误卡片，其余页签与外壳照常可用；
 * 2. 报错信息（含组件栈）直接显示在页面上并同时打进 console，可追溯而不是"看运气复现"。
 * 它不掩盖 bug，只保证故障可见、可定位、不会变成一片空白。
 */
interface Props {
  /** 页签标识，用于 key 与错误提示文案。 */
  tab: string;
  children: React.ReactNode;
}

interface State {
  error: Error | null;
  stack: string | null;
}

export class TabErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null, stack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    this.setState({ stack: info.componentStack ?? null });
    // 保留原始报错，方便在 devtools 里看完整堆栈。
    console.error(`[koxpilot] 页签 #${this.props.tab} 渲染失败：`, error, info.componentStack);
  }

  componentDidUpdate(prev: Props): void {
    if (prev.tab !== this.props.tab && this.state.error) this.setState({ error: null, stack: null });
  }

  render(): React.ReactNode {
    const { error, stack } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="card px-5 py-4">
        <div className="text-[13px] font-semibold text-rose-700">
          页签 #{this.props.tab} 渲染失败（页面没有白屏，这里如实报错）
        </div>
        <p className="muted mt-1.5">
          {error.name}: {error.message}
        </p>
        <p className="muted mt-2">
          其余页签仍可正常切换。这层错误边界的存在本身也是一条工程纪律：渲染异常必须变成一张可读的卡片，
          而不是一片无法自证的空白。完整堆栈同时打在浏览器 console 里。
        </p>
        {stack && (
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-100 p-2 font-mono text-[12px] leading-relaxed text-slate-700">
            {stack.trim()}
          </pre>
        )}
      </div>
    );
  }
}
