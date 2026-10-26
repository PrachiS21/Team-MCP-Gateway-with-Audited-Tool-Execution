# The official MCP reference server ("everything"), used as an upstream we did NOT write.
# Build from this folder:  docker build -t everything-mcp:dev .
# The exact version is pinned in package.json and package-lock.json; `npm ci` installs exactly that.
#
# Treat it as untrusted: it has a tool that returns all its environment variables (so this pod holds no secrets)
# and a tool that fetches any URL (so its NetworkPolicy allows no outbound traffic). It has no authentication,
# so only the NetworkPolicy keeps other pods away from it.

FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
ENV PORT=3001 NODE_ENV=production
USER 1000:1000
EXPOSE 3001
CMD ["node", "/app/node_modules/@modelcontextprotocol/server-everything/dist/index.js", "streamableHttp"]
