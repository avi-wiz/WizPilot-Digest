FROM python:3.12-slim
WORKDIR /app
COPY . .
# requirements.txt is intentionally empty (see its header) — this is a no-op,
# kept so the build step is explicit rather than skipped silently.
RUN pip install --no-cache-dir -r requirements.txt
ENTRYPOINT ["python", "poc_digest.py"]
