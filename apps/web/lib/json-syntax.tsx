// Render a JS value as syntax-colored JSON. One recursive walker emits <span>
// with stable class names ('jsonKey' etc.) the CSS Module styles via --json-* tokens.
import { Fragment, type ReactNode } from 'react';
import styles from '@/components/ui/JsonPanel.module.css';

const pad = (depth: number) => '  '.repeat(depth);

function scalar(value: unknown): ReactNode {
  if (typeof value === 'string') return <span className={styles.jsonString}>&quot;{value}&quot;</span>;
  if (typeof value === 'number') return <span className={styles.jsonNumber}>{String(value)}</span>;
  if (typeof value === 'boolean') return <span className={styles.jsonBool}>{String(value)}</span>;
  if (value === null) return <span className={styles.jsonBool}>null</span>;
  return <span>{String(value)}</span>;
}

function walk(value: unknown, depth: number, key?: number): ReactNode {
  const punct = (s: string) => <span className={styles.jsonPunct}>{s}</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <Fragment key={key}>{punct('[]')}</Fragment>;
    return (
      <Fragment key={key}>
        {punct('[')}{'\n'}
        {value.map((v, i) => (
          <Fragment key={i}>
            {pad(depth + 1)}{walk(v, depth + 1)}{i < value.length - 1 ? punct(',') : null}{'\n'}
          </Fragment>
        ))}
        {pad(depth)}{punct(']')}
      </Fragment>
    );
  }

  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <Fragment key={key}>{punct('{}')}</Fragment>;
    return (
      <Fragment key={key}>
        {punct('{')}{'\n'}
        {entries.map(([k, v], i) => (
          <Fragment key={k}>
            {pad(depth + 1)}<span className={styles.jsonKey}>&quot;{k}&quot;</span>{punct(': ')}
            {walk(v, depth + 1)}{i < entries.length - 1 ? punct(',') : null}{'\n'}
          </Fragment>
        ))}
        {pad(depth)}{punct('}')}
      </Fragment>
    );
  }

  return <Fragment key={key}>{scalar(value)}</Fragment>;
}

export function renderJson(value: unknown): ReactNode {
  return walk(value, 0);
}
