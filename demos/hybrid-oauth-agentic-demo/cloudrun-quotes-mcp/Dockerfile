FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "fastmcp>=2.0.0" "starlette>=0.41.0" "uvicorn>=0.34.0"

COPY main.py .
COPY toolspec.json .

ENV BIND_HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
