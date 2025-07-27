# Build stage with explicit platform specification
FROM ghcr.io/astral-sh/uv:python3.12-alpine AS uv

# Install the project into /app
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
ADD . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Final stage with Node.js support for MCP servers
FROM node:20-alpine

# Install Python and other dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    git \
    curl \
    && ln -sf python3 /usr/bin/python

# Create app user
RUN addgroup -g 1001 -S app && \
    adduser -S app -u 1001 -G app

# Copy the virtual environment from build stage
COPY --from=uv --chown=app:app /app/.venv /app/.venv

# Create app directory and set ownership
WORKDIR /app
RUN chown -R app:app /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Install commonly used MCP servers globally
RUN npm install -g \
    @modelcontextprotocol/server-github \
    @modelcontextprotocol/server-filesystem \
    @modelcontextprotocol/server-brave-search

# Install uv for dynamic MCP server installation (uvx is included with uv)
RUN pip install --no-cache-dir uv

# Ensure uv cache directory exists and is writable
RUN mkdir -p /tmp/uv-cache && chmod 777 /tmp/uv-cache
ENV UV_CACHE_DIR=/tmp/uv-cache

# Switch to app user
USER app

# Create config directory
RUN mkdir -p /app/config

# Expose the default bridge port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/status || exit 1

# Default command
ENTRYPOINT ["mcp-foxxy-bridge"]
CMD ["--port", "8080", "--host", "0.0.0.0"]
