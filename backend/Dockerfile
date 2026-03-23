FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -r -u 1001 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

LABEL org.opencontainers.image.source="https://github.com/georg-nikola/tf-registry"
LABEL org.opencontainers.image.description="Terraform Module Registry API"
LABEL org.opencontainers.image.licenses="MIT"
