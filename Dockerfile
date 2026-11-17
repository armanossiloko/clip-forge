# ClipForge: everything in one image (Python app, ffmpeg, Node for YouTube + Claude Code).
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/app/models \
    CLIPFORGE_DOCKER=1 \
    CLIPFORGE_VIDEOS_DIR=/videos \
    CLIPFORGE_CAPTION_FONT=Poppins \
    DISABLE_AUTOUPDATER=1

# ffmpeg (Debian's build includes libass for burned-in captions), Python, fonts
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv ffmpeg fontconfig fonts-dejavu-core ca-certificates libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. It logs in with CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY.
RUN npm install -g @anthropic-ai/claude-code \
 && echo '{"hasCompletedOnboarding": true}' > /root/.claude.json \
 && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && pip install --upgrade pip && pip install -r requirements.txt

# Caption font, also installed system-wide so fontconfig/libass find it by name
COPY fonts/ /app/fonts/
RUN mkdir -p /usr/share/fonts/truetype/clipforge \
 && cp /app/fonts/*.ttf /usr/share/fonts/truetype/clipforge/ \
 && fc-cache -f

COPY app/ /app/app/
COPY run.py /app/
RUN mkdir -p /app/data /app/models /videos

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
