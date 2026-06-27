FROM python:3.12-slim

LABEL org.opencontainers.image.title="Khan"
LABEL org.opencontainers.image.description="Local multi-agent decision system for coding work"
LABEL org.opencontainers.image.source="https://github.com/nirmalhk7/khan"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION README.md ./
COPY src ./src

ENV PYTHONPATH=/app/src
ENV KHAN_HOME=/data

RUN useradd --create-home --uid 10001 khan \
    && mkdir -p /data \
    && chown -R khan:khan /data /app

USER khan
VOLUME ["/data"]
ENTRYPOINT ["python", "/app/src/khan_cli.py"]
CMD ["--help"]
