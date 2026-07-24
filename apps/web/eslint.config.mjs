import nextPlugin from '@next/eslint-plugin-next/dist/index.js';
import tsPlugin from '@typescript-eslint/eslint-plugin';
import tsParser from '@typescript-eslint/parser';
import reactHooks from 'eslint-plugin-react-hooks';

// ESLint 9 flat config. eslint-config-next@15.1.0's legacy entry require()s
// @rushstack/eslint-patch, which cannot hook ESLint 9 under pnpm's layout
// ("Failed to patch ESLint…"); use Next's lint plugin directly instead.
// @typescript-eslint/recommended gives the gate real teeth (no-explicit-any,
// no-unused-vars) on top of Next's rules.
export default [
  { ignores: ['.next/**', 'node_modules/**', 'coverage/**', 'playwright-report/**'] },
  {
    files: ['**/*.{js,jsx,ts,tsx,mjs}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      '@next/next': nextPlugin,
      '@typescript-eslint': tsPlugin,
      // Registered so `eslint-disable react-hooks/*` directives in the codebase resolve
      // (the rule is otherwise "not found" under flat config and fails `next build`).
      'react-hooks': reactHooks,
    },
    rules: {
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs['core-web-vitals'].rules,
      ...tsPlugin.configs.recommended.rules,
      // exhaustive-deps kept at 'warn' (not the recommended 'error') so a missing hook
      // dependency never fails the production build; rules-of-hooks stays off to avoid
      // surfacing unrelated errors — the sole purpose here is to define the rule name so
      // existing disable directives are valid.
      'react-hooks/exhaustive-deps': 'warn',
      // Honor the `_`-prefixed intentionally-unused convention the codebase already uses
      // (e.g. fetch-mock signatures like `(_url) => …`), mirroring TS's noUnused* behavior.
      // This only LOOSENS no-unused-vars — it can never introduce a new error.
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      }],
    },
  },
];
