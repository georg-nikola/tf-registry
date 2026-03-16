# Build stage - validate static files exist and are non-empty
FROM alpine:3.21 AS validator

COPY index.html /validate/
COPY module.html /validate/
COPY upload.html /validate/
COPY keys.html /validate/
COPY style.css /validate/
COPY app.js /validate/

RUN test -s /validate/index.html && \
    test -s /validate/module.html && \
    test -s /validate/upload.html && \
    test -s /validate/keys.html && \
    test -s /validate/style.css && \
    test -s /validate/app.js

# Production stage
FROM nginx:stable-alpine3.23

RUN addgroup -g 101 -S nginx || true && \
    adduser -S -D -H -u 101 -h /var/cache/nginx -s /sbin/nologin -G nginx -g nginx nginx || true

RUN rm -rf /etc/nginx/conf.d/default.conf /usr/share/nginx/html/*

COPY nginx.conf /etc/nginx/nginx.conf

COPY --from=validator /validate/index.html /usr/share/nginx/html/
COPY --from=validator /validate/module.html /usr/share/nginx/html/
COPY --from=validator /validate/upload.html /usr/share/nginx/html/
COPY --from=validator /validate/keys.html /usr/share/nginx/html/
COPY style.css /usr/share/nginx/html/
COPY app.js /usr/share/nginx/html/
COPY favicon.ico /usr/share/nginx/html/

RUN echo "OK" > /usr/share/nginx/html/health

RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chmod -R 755 /usr/share/nginx/html && \
    chown -R nginx:nginx /var/cache/nginx && \
    chown -R nginx:nginx /var/log/nginx && \
    touch /var/run/nginx.pid && \
    chown nginx:nginx /var/run/nginx.pid

USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1

CMD ["nginx", "-g", "daemon off;"]

LABEL org.opencontainers.image.source="https://github.com/georg-nikola/tf-registry"
LABEL org.opencontainers.image.description="Terraform Module Registry - static frontend"
LABEL org.opencontainers.image.licenses="MIT"
