FROM heartexlabs/label-studio:1.23.0 AS ls-source

FROM nginx:1.27-alpine

# 保持原路径，nginx.conf 里的 alias 不用改
COPY --from=ls-source /label-studio/label_studio/core/static_build/ \
                      /label-studio/label_studio/core/static_build/
COPY --from=ls-source /label-studio/web/dist/apps/labelstudio/ \
                      /label-studio/web/dist/apps/labelstudio/

# 只服务静态文件的 nginx 配置
# COPY nginx-static.conf /etc/nginx/nginx.conf

# RUN mkdir -p /tmp/proxy_temp /tmp/client_temp /tmp/fastcgi_temp \
#              /tmp/uwsgi_temp /tmp/scgi_temp /var/cache/nginx \
#     && chown -R nginx:nginx /tmp /var/cache/nginx

EXPOSE 8085
CMD ["nginx", "-g", "daemon off;", "-c", "/etc/nginx/nginx.conf"]