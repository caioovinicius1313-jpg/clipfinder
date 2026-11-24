FROM denoland/deno:bin-2.9.8 AS deno_runtime

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# yt-dlp recommends Deno for solving YouTube's JavaScript player challenges.
COPY --from=deno_runtime /deno /usr/local/bin/deno
RUN deno --version

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import importlib.metadata as m; print('yt-dlp', m.version('yt-dlp')); print('yt-dlp-ejs', m.version('yt-dlp-ejs'))"

COPY . .
RUN mkdir -p /app/storage

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
