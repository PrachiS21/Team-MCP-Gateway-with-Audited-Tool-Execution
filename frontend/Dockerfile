# Dashboard image. Build from this folder:  docker build -t dashboard:dev .
# Stage 1 builds the static files with Node; stage 2 serves them with an unprivileged nginx (no Node at run time).

FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

# Cloud Run (docker build --target cloudrun): the same files, with /api/ forwarded to the gateway's own Cloud Run
# address (nginx.cloudrun.conf.template, filled in at start). Kubernetes builds the last stage below, unchanged.
FROM nginxinc/nginx-unprivileged:1.28-alpine AS cloudrun
COPY nginx.cloudrun.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
# Fill in only GATEWAY_URL, GATEWAY_HOST and NGINX_LOCAL_RESOLVERS (the container's DNS server, found at start);
# nginx's own lowercase $variables are never touched.
ENV NGINX_ENVSUBST_FILTER="^(GATEWAY_|NGINX_LOCAL_RESOLVERS)" NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
EXPOSE 8080

# Kubernetes (the default target): static files only; the Ingress sends /api and /mcp to the gateway.
FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Runs as the image's non-root user (uid 101), listens on 8080, writes only to /tmp (an emptyDir in the cluster).
EXPOSE 8080
