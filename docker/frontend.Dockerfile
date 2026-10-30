# Atlas dashboard development server.
# Build context: ./frontend

FROM node:22-bookworm-slim

ENV CI=true
WORKDIR /app

# pnpm version is pinned by "packageManager" in package.json.
RUN corepack enable && corepack prepare pnpm@12.3.4 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

RUN chown -R node:node /app
USER node

EXPOSE 5173
CMD ["pnpm", "dev"]
