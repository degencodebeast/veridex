# apps/web/Dockerfile — D-0 STATIC Next.js image (Wave 0 scaffold).
#
# Build context = apps/web (see compose.coolify.yml); apps/web/.dockerignore
# governs this context. Standalone pnpm project (own pnpm-lock.yaml), so the
# app dir is a self-sufficient context — no monorepo root needed.
#
# STATIC scaffold: authored before any app dependency, wired/built at D-1.
# NEXT_PUBLIC_* build args are injected via Coolify in D-1, never committed.

# ── deps: install with the lockfile only, for layer-cache stability ──────
FROM node:22-alpine AS deps
WORKDIR /app
RUN corepack enable pnpm
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

# ── build: compile the Next.js app ───────────────────────────────────────
FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable pnpm
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ── runner: production-only artifacts, non-root ──────────────────────────
FROM node:22-alpine AS runner
WORKDIR /app
RUN corepack enable pnpm
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile --prod
COPY --from=build /app/.next ./.next
COPY next.config.mjs ./
USER node
EXPOSE 3000
CMD ["pnpm", "start"]
