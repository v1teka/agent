FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY proactive ./proactive
RUN pip install --no-cache-dir .
CMD ["python", "-m", "proactive"]
