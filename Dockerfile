FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pr_sentinel ./pr_sentinel

EXPOSE 8000
CMD ["uvicorn", "pr_sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
