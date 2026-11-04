# syntax=docker/dockerfile:1.7
# AI 3D Studio web app (Next.js standalone). Build from the repository root:
#   docker build -f infrastructure/docker/web.Dockerfile -t ai3d-web .
# AI3D_API_URL is the backend origin the /api/v1 proxy forwards to; it is read
# when the app is built, so set it as a build argument.

FROM node:24-slim AS build
ENV NEXT_TELEMETRY_DISABLED=1 COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /repo

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/web/package.json apps/web/
COPY packages/ui/package.json packages/ui/
COPY packages/studio/package.json packages/studio/
COPY packages/api-client/package.json packages/api-client/
COPY packages/shared-types/package.json packages/shared-types/
COPY packages/utilities/package.json packages/utilities/
COPY packages/viewer/package.json packages/viewer/
COPY packages/editor/package.json packages/editor/
RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter "@ai3d/web..."

COPY packages packages
COPY apps/web apps/web
ARG AI3D_API_URL=http://backend:8000
ENV AI3D_API_URL=$AI3D_API_URL
RUN pnpm --filter @ai3d/web build

FROM node:24-slim
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /repo/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=node:node /repo/apps/web/public ./apps/web/public
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "apps/web/server.js"]
