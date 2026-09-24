FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt .

USER 10001
EXPOSE 8080
# build_app() reads the cluster config and environment at start-up, not at import time.
CMD ["uvicorn", "agentic_ops.web:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
