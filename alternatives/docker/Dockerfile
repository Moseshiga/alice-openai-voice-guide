FROM python:3.12-slim
WORKDIR /app
COPY api /app/api
COPY alternatives/docker/serve.py /app/serve.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "/app/serve.py"]

