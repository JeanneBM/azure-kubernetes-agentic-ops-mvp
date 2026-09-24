FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt .

USER 10001
EXPOSE 8080
# The deployed manifest sets AGENTIC_OPS_ROLE to diagnostic or remediation.
CMD ["uvicorn", "agentic_ops.split_app:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
