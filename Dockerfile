FROM docker.io/library/python:3.11.10-slim
LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-higgsfield"
LABEL org.opencontainers.image.version="0.5.11"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY install_cli.py /app/install_cli.py
RUN python /app/install_cli.py /usr/local/bin/hf && rm /app/install_cli.py
COPY pyproject.toml ./
COPY higgsfield_gateway ./higgsfield_gateway
COPY dashboard ./dashboard-source
RUN python -m higgsfield_gateway.dashboard --source /app/dashboard-source --output /app/dashboard && rm -rf /app/dashboard-source
RUN pip install --no-cache-dir . && pip check && useradd --uid 10001 --no-create-home gateway
USER 10001:10001
EXPOSE 8000 8001
CMD ["python", "-m", "higgsfield_gateway.runtime"]
