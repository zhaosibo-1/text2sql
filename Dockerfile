FROM python:3.12-slim

WORKDIR /srv

# 先拷依赖清单再装依赖：requirements 不变就是缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# 演示库在构建期生成，镜像开箱即用
RUN python -c "from app.seed import build; build('/srv/data/demo.db')"

RUN useradd --create-home runner && chown -R runner /srv
USER runner

EXPOSE 8133
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8133/api/health',timeout=2)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8133"]
