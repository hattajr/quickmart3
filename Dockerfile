# Use the official uv Docker image for Python 3.13
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS app

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY app/ ./app/
# migrations/ is a one-shot script run manually on the host, not shipped in the image

# Create non-root user and change ownership
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose the port from .env.prod
EXPOSE 8756

# Run the application with uv
# The Tailwind CSS compilation will happen on startup in main.py
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8756", "--workers", "1"]

# The invoice photo reader: the app image plus headless Claude Code (`claude -p`).
# It signs in with CLAUDE_CODE_OAUTH_TOKEN (made by `claude setup-token`), passed at runtime.
FROM app AS invoice-worker
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
USER appuser
# Pinned so a rebuild reads invoices the same way; bump it on purpose.
ARG CLAUDE_CODE_VERSION=2.1.329
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" && \
    /home/appuser/.local/bin/claude --version
ENV PATH="/home/appuser/.local/bin:$PATH" \
    DISABLE_AUTOUPDATER=1
CMD ["uv", "run", "--no-dev", "python", "-m", "app.services.supplier_invoices.worker", "--db", "/data/quickmart.sqlite3"]
