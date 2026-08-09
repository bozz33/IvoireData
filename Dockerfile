FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY registry ./registry
COPY configs ./configs
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "ivoiredata.api:app", "--host", "0.0.0.0", "--port", "8000"]
