FROM python:3.11-slim

RUN groupadd -r thronos && useradd -r -g thronos -d /app -s /sbin/nologin thronos

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R thronos:thronos /app

USER thronos

ENV PORT=8000

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
