# Frontend image: build the React app, serve it with nginx.
# nginx also reverse-proxies /api to the backend service (see nginx.conf),
# so the browser talks to a single origin (no CORS in production).

FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
