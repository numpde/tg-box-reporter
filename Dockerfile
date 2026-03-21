FROM docker:cli AS docker_cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

COPY pyproject.toml /app/pyproject.toml
COPY tg_box_reporter /app/tg_box_reporter

RUN pip install --no-cache-dir .

CMD ["python", "-m", "tg_box_reporter.collector"]
