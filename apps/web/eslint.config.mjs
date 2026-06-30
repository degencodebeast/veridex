import nextPlugin from '@next/eslint-plugin-next/dist/index.js';
import tsParser from '@typescript-eslint/parser';

// ESLint 9 flat config. eslint-config-next@15.1.0's legacy entry require()s
// @rushstack/eslint-patch, which cannot hook ESLint 9 under pnpm's layout
// ("Failed to patch ESLint…"); use Next's lint plugin directly instead.
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
    plugins: { '@next/next': nextPlugin },
    rules: {
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs['core-web-vitals'].rules,
    },
  },
];
