FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock requirements.acceptance.txt ./
RUN pip install --no-cache-dir -r requirements.lock -r requirements.acceptance.txt && python -m playwright install --with-deps chromium
COPY . .
CMD ["python", "-m", "acceptance.linux_proof"]
