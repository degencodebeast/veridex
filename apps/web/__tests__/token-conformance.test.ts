import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve, basename } from 'node:path';

const ROOT = resolve(__dirname, '..');
const SCAN_DIRS = ['app', 'components', 'hooks', 'lib', 'styles'];
const HEX = /#[0-9a-fA-F]{3,8}\b/;
const EXEMPT = new Set(['tokens.css']);

function walk(dir: string): string[] {
  let out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out = out.concat(walk(full));
    else if (/\.(css|ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

describe('token conformance (PAT-001: no raw hex outside the token source)', () => {
  it('declares the required Direction A color tokens', () => {
    const tokens = readFileSync(join(ROOT, 'styles/tokens.css'), 'utf8');
    for (const t of ['--bg:', '--panel:', '--border:', '--text-1:', '--accent:', '--warning:', '--positive:', '--negative:']) {
      expect(tokens, `missing ${t}`).toContain(t);
    }
    expect(tokens).toContain('#070A0E');
    expect(tokens).toContain('#3B82F6');
  });

  it('contains no raw hex color outside tokens.css', () => {
    const offenders: string[] = [];
    for (const dir of SCAN_DIRS) {
      const abs = join(ROOT, dir);
      if (!existsSync(abs)) continue; // scan dir not created yet (built in a later plan)
      for (const file of walk(abs)) {
        if (EXEMPT.has(basename(file))) continue;
        const text = readFileSync(file, 'utf8');
        text.split('\n').forEach((line, i) => {
          if (HEX.test(line)) offenders.push(`${file}:${i + 1}  ${line.trim()}`);
        });
      }
    }
    expect(offenders, `raw hex found:\n${offenders.join('\n')}`).toEqual([]);
  });
});
