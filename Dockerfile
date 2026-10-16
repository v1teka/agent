FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
RUN pip install --no-cache-dir .
CMD ["python", "-m", "agent"]
