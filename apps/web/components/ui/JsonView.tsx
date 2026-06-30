import { Fragment, type ReactNode } from 'react';
import styles from './JsonView.module.css';

// GUD-002: render from a data object with a tiny syntax-color helper — never hand-write spans.
function renderNode(value: unknown, indent: number): ReactNode {
  const pad = '  '.repeat(indent);
  if (value === null) return <span className={styles.null}>null</span>;
  if (typeof value === 'string') return <span className={styles.str}>&quot;{value}&quot;</span>;
  if (typeof value === 'number') return <span className={styles.num}>{value}</span>;
  if (typeof value === 'boolean') return <span className={styles.bool}>{String(value)}</span>;
  if (Array.isArray(value)) {
    return (
      <>
        {'[\n'}
        {value.map((v, i) => (
          <Fragment key={i}>
            {pad}{'  '}{renderNode(v, indent + 1)}{i < value.length - 1 ? ',' : ''}{'\n'}
          </Fragment>
        ))}
        {pad}{']'}
      </>
    );
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <>
        {'{\n'}
        {entries.map(([k, v], i) => (
          <Fragment key={k}>
            {pad}{'  '}<span className={styles.key}>&quot;{k}&quot;</span>
            <span className={styles.punct}>: </span>{renderNode(v, indent + 1)}
            {i < entries.length - 1 ? ',' : ''}{'\n'}
          </Fragment>
        ))}
        {pad}{'}'}
      </>
    );
  }
  return <span>{String(value)}</span>;
}

export function JsonView({ data }: { data: unknown }) {
  return <pre className={`${styles.pre} mono`} tabIndex={-1}>{renderNode(data, 0)}</pre>;
}
