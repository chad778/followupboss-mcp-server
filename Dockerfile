FROM node:20-alpine

WORKDIR /app

# Install deps first for better layer caching
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --no-audit --no-fund

# Copy source
COPY index.js setup.js ./

# Railway sets PORT automatically. Default for local docker testing.
ENV PORT=3000
ENV MCP_TRANSPORT=http

EXPOSE 3000

# Health check hits /health (no auth required)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://localhost:' + (process.env.PORT||3000) + '/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "index.js"]
