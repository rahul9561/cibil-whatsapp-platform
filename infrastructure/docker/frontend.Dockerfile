# Build context: frontend/
# Stage 1 builds the SPA; stage 2 is a small nginx image with only the static files.
# The nginx configuration is mounted at runtime from infrastructure/nginx (it differs per environment).

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm test && npm run build


FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
# Default site is replaced by the mounted template (see docker-compose.prod.yml).
RUN rm -f /etc/nginx/conf.d/default.conf
