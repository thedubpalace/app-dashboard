FROM python:3.12-slim

# Install docker CLI so docker_service.py can shell out to `docker`
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
         https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Path to the shared app registry — override at runtime via -e or docker-compose
ENV APP_DASHBOARD_REGISTRY=/veda/.veda/projects/registry.json

EXPOSE 8765

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
