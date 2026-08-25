FROM node:24-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src

ARG VITE_RECEIVER_LAB_BASE_URL=http://receiver-lab:8100/webhooks
ENV VITE_RECEIVER_LAB_BASE_URL=${VITE_RECEIVER_LAB_BASE_URL}

RUN npm run build


FROM nginxinc/nginx-unprivileged:1.28-alpine AS runtime

ENV API_UPSTREAM=http://api:8000

COPY --chown=101:101 nginx.conf /etc/nginx/templates/default.conf.template
COPY --chown=101:101 --from=build /app/dist /usr/share/nginx/html

USER 101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/healthz || exit 1
