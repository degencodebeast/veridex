# apps/web/Dockerfile — D-0 STATIC Next.js image (Wave 0 scaffold).
#
# Build context = apps/web (see compose.coolify.yml); apps/web/.dockerignore
# governs this context. Standalone pnpm project (own pnpm-lock.yaml), so the
# app dir is a self-sufficient context — no monorepo root needed.
#
# STATIC scaffold: authored before any app dependency, wired/built at D-1.
# NEXT_PUBLIC_* build args are injected via Coolify in D-1, never committed.

# ── base: node + PINNED pnpm, shared by every stage ──────────────────────
# pnpm is pinned to an exact 9.x: pnpm-lock.yaml is lockfileVersion '9.0'
# (the pnpm-9 format), and pnpm 10 changed lifecycle-script defaults — a
# floating corepack resolution would let the interpreter of
# --frozen-lockfile drift across builds with zero repo diff. One base stage
# keeps the pin identical everywhere pnpm runs.
FROM node:22-alpine AS base
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@9.15.4 --activate

# ── deps: install with the lockfile only, for layer-cache stability ──────
FROM base AS deps
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

# ── build: compile the Next.js app ───────────────────────────────────────
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# .next/cache is build-machine cache — dead weight in the runtime image.
RUN pnpm build && rm -rf .next/cache

# ── runner: production-only artifacts, non-root ──────────────────────────
FROM base AS runner
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile --prod
# --chown: Next writes .next/cache at runtime (ISR / image optimization);
# a root-owned .next would EACCES under USER node.
COPY --from=build --chown=node:node /app/.next ./.next
COPY next.config.mjs ./
# apps/web has no public/ today. The day it appears, uncomment — otherwise
# the image silently drops static assets with no build error:
# COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
# Invoke the next binary directly — pnpm/corepack must NOT be on the runtime
# path (corepack under USER node would re-download pnpm from the registry at
# container start, making production startup depend on registry egress).
CMD ["node_modules/.bin/next", "start"]
