FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --gid 10001 farstar && useradd --uid 10001 --gid farstar --no-create-home farstar
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml ./
COPY farstar ./farstar
RUN pip install --no-deps . && mkdir -p /app/data && chown -R farstar:farstar /app/data
USER farstar
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "-m", "farstar", "run"]
