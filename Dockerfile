FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

ARG TORCH_VERSION=2.8.0
ARG EMBEDDING_MODEL_REVISION=01d3c3cd65ac9dc6bd0d702ed913366e7931097b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    COENGRAM_EMBEDDING_MODEL=/opt/coengram/models/bge-small-en-v1.5

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /models \
    && chown app:app /models

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic-control.ini alembic-tenant.ini ./

RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==${TORCH_VERSION}" \
    && pip install --no-cache-dir . \
    && HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c \
        "from huggingface_hub import snapshot_download; snapshot_download(repo_id='BAAI/bge-small-en-v1.5', revision='${EMBEDDING_MODEL_REVISION}', local_dir='/opt/coengram/models/bge-small-en-v1.5', allow_patterns=('*.json', '*.txt', '*.safetensors', '1_Pooling/*'))" \
    && chown -R app:app /opt/coengram/models

USER app

EXPOSE 8080

CMD ["python", "-m", "agent_memory_service.server"]
