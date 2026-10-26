# Dashboard image. Build from this folder:  docker build -t dashboard:dev .
# Stage 1 builds the static files with Node; stage 2 serves them with an unprivileged nginx (no Node at run time).

FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Runs as the image's non-root user (uid 101), listens on 8080, writes only to /tmp (an emptyDir in the cluster).
EXPOSE 8080
