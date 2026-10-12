FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home accord
COPY --chown=accord:accord . .
USER accord
EXPOSE 8000
CMD ["uvicorn", "accord.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

