FROM python:3.12-slim

# UI only: the page plus an /api relay to the host agent (agent.py), which
# must run natively on the Windows host. See docker-compose.yml.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV APP_AGENT_URL=http://host.docker.internal:8766

EXPOSE 8765

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
