FROM python:3.12-slim-bookworm

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# 复制应用代码
COPY subtranslator.py .
COPY web_ui.py .
COPY config_manager.py .
COPY templates/ ./templates/
COPY static/ ./static/

# 创建配置和日志目录
RUN mkdir -p /config

# 设置卷
VOLUME ["/config", "/mnt/user/media"]

# 设置默认环境变量（非敏感信息）
# API_KEY 应在运行时通过 docker run -e 或 docker-compose 传入
ENV PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    MOVIES_DIR="/mnt/user/media/media/movies" \
    TV_DIR="/mnt/user/media/media/tv" \
    CUSTOM_DIRS="" \
    MAX_WORKERS="10" \
    API_RETRY_COUNT="3" \
    LOG_LEVEL="INFO" \
    WEB_PORT="8095" \
    TARGET_LANG="zh"

# 暴露 Web UI 端口
EXPOSE 8095

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; from config_manager import load_config; urllib.request.urlopen('http://localhost:%s/health' % load_config()['WEB_PORT'], timeout=5)"]

# 运行应用（使用 root 用户避免权限问题）
CMD ["python", "subtranslator.py"]
