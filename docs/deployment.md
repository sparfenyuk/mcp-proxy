# Deployment Guide

This guide covers different ways to deploy MCP Foxxy Bridge in various environments.

## Deployment Options

### 1. Local Development

For development and testing:

```bash
# Install dependencies
uv sync

# Run with development config
uv run mcp-foxxy-bridge --bridge-config bridge_config_example.json

# Enable debug logging
uv run mcp-foxxy-bridge --bridge-config config.json --debug

# Custom port
uv run mcp-foxxy-bridge --bridge-config config.json --port 8081
```

### 2. Production Local Process

For production deployment on a single server:

```bash
# Install as tool
uv tool install mcp-foxxy-bridge

# Create production config
cat > production_config.json << 'EOF'
{
  "mcpServers": {
    "github": {
      "enabled": true,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "toolNamespace": "github"
    }
  },
  "bridge": {
    "conflictResolution": "namespace"
  }
}
EOF

# Set environment variables
export GITHUB_TOKEN=your_token_here

# Run bridge
mcp-foxxy-bridge --bridge-config production_config.json --port 8080
```

### 3. Docker Container

#### Simple Docker Run

```bash
# Build image
docker build -t mcp-foxxy-bridge .

# Run container
docker run -d \
  --name mcp-bridge \
  -p 8080:8080 \
  -v $(pwd)/config.json:/app/config/config.json:ro \
  -e GITHUB_TOKEN=your_token \
  -e BRAVE_API_KEY=your_api_key \
  mcp-foxxy-bridge --bridge-config /app/config/config.json
```

#### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  mcp-foxxy-bridge:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./bridge_config_docker.json:/app/config/bridge_config.json:ro
      - ./data:/app/data:ro  # For filesystem server
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - BRAVE_API_KEY=${BRAVE_API_KEY}
    command:
      - "--bridge-config"
      - "/app/config/bridge_config.json"
      - "--port"
      - "8080"
      - "--host"
      - "0.0.0.0"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/status"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

Deploy with:

```bash
# Create .env file
cat > .env << 'EOF'
GITHUB_TOKEN=your_token_here
BRAVE_API_KEY=your_api_key_here
EOF

# Start services
docker-compose up -d

# View logs
docker-compose logs -f mcp-foxxy-bridge

# Stop services
docker-compose down
```

### 4. UV Tool Installation

System-wide installation using UV:

```bash
# Install globally
uv tool install mcp-foxxy-bridge

# Create systemd service (Linux)
sudo tee /etc/systemd/system/mcp-bridge.service > /dev/null << 'EOF'
[Unit]
Description=MCP Foxxy Bridge
After=network.target

[Service]
Type=simple
User=mcp-bridge
Group=mcp-bridge
WorkingDirectory=/opt/mcp-bridge
Environment=GITHUB_TOKEN=your_token
Environment=BRAVE_API_KEY=your_api_key
ExecStart=/home/mcp-bridge/.local/bin/mcp-foxxy-bridge \
  --bridge-config /opt/mcp-bridge/config.json --port 8080
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Create user and directory
sudo useradd -r -d /opt/mcp-bridge -s /bin/false mcp-bridge
sudo mkdir -p /opt/mcp-bridge
sudo chown mcp-bridge:mcp-bridge /opt/mcp-bridge

# Copy config file
sudo cp config.json /opt/mcp-bridge/
sudo chown mcp-bridge:mcp-bridge /opt/mcp-bridge/config.json

# Start service
sudo systemctl enable mcp-bridge
sudo systemctl start mcp-bridge
sudo systemctl status mcp-bridge
```

## Environment Configuration

### Environment Variables

Set these before running the bridge:

```bash
# Required for GitHub server
export GITHUB_TOKEN=ghp_your_personal_access_token

# Required for Brave Search server
export BRAVE_API_KEY=your_brave_api_key

# Optional: Custom port
export PORT=8080

# Optional: Debug mode
export DEBUG=true
```

### Configuration Files

Different configs for different environments:

```bash
# Development
mcp-foxxy-bridge --bridge-config dev_config.json

# Staging  
mcp-foxxy-bridge --bridge-config staging_config.json

# Production
mcp-foxxy-bridge --bridge-config production_config.json
```

## Health Monitoring

### Status Endpoint

Monitor bridge health:

```bash
# Check overall status
curl http://localhost:8080/status

# Pretty print JSON
curl -s http://localhost:8080/status | python -m json.tool

# Monitor continuously
watch -n 5 'curl -s http://localhost:8080/status | jq .'
```

### Logging

Configure logging for monitoring:

```bash
# Run with debug logging
mcp-foxxy-bridge --bridge-config config.json --debug

# Log to file
mcp-foxxy-bridge --bridge-config config.json 2>&1 | tee bridge.log

# Rotate logs with logrotate
sudo tee /etc/logrotate.d/mcp-bridge > /dev/null << 'EOF'
/var/log/mcp-bridge.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 644 mcp-bridge mcp-bridge
    postrotate
        systemctl reload mcp-bridge
    endscript
}
EOF
```

## Load Balancing

For high availability, run multiple bridge instances:

### Nginx Load Balancer

```nginx
upstream mcp_bridge {
    server localhost:8080;
    server localhost:8081;
    server localhost:8082;
}

server {
    listen 80;
    server_name mcp-bridge.example.com;
    
    location / {
        proxy_pass http://mcp_bridge;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        
        # SSE specific settings
        proxy_cache off;
        proxy_buffering off;
        proxy_read_timeout 24h;
    }
    
    location /status {
        proxy_pass http://mcp_bridge;
        proxy_set_header Host $host;
    }
}
```

### Docker Swarm

```yaml
version: '3.8'

services:
  mcp-foxxy-bridge:
    image: mcp-foxxy-bridge:latest
    deploy:
      replicas: 3
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 3
    ports:
      - "8080:8080"
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
    configs:
      - source: bridge_config
        target: /app/config/bridge_config.json

configs:
  bridge_config:
    file: ./bridge_config_docker.json
```

## Security Considerations

### Network Security

```bash
# Bind to localhost only (default)
mcp-foxxy-bridge --bridge-config config.json --host 127.0.0.1

# Bind to all interfaces (use with firewall)
mcp-foxxy-bridge --bridge-config config.json --host 0.0.0.0

# Use firewall to restrict access
sudo ufw allow from 10.0.0.0/8 to any port 8080
```

### Secrets Management

Never put secrets in config files:

```json
{
  "env": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",  // ✅ Good
    "API_KEY": "hardcoded_secret"                       // ❌ Bad
  }
}
```

### TLS Termination

Use a reverse proxy for HTTPS:

```bash
# With Caddy
echo "mcp-bridge.example.com {
  reverse_proxy localhost:8080
}" > Caddyfile

caddy run
```

## Performance Tuning

### Resource Limits

Docker resource limits:

```yaml
services:
  mcp-foxxy-bridge:
    # ... other config
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
        reservations:
          memory: 256M
          cpus: '0.25'
```

### Configuration Optimization

```json
{
  "bridge": {
    "failover": {
      "enabled": true,
      "maxFailures": 2,        // Fail fast
      "recoveryInterval": 30000 // Quick recovery
    }
  },
  "mcpServers": {
    "example": {
      "timeout": 30,           // Shorter timeout
      "retryAttempts": 2,      // Fewer retries
      "healthCheck": {
        "interval": 60000      // Less frequent checks
      }
    }
  }
}
```

## Troubleshooting Deployment

### Common Issues

1. **Port already in use**: The bridge auto-increments ports
2. **Permission denied**: Check file permissions and user access
3. **Environment variables not found**: Verify variable names and values
4. **MCP servers not connecting**: Check network access and dependencies

### Debug Mode

```bash
# Enable debug logging
mcp-foxxy-bridge --bridge-config config.json --debug

# Check specific server connectivity
mcp-foxxy-bridge --bridge-config minimal_config.json --debug
```

### Health Checks

```bash
# Test connectivity
curl -v http://localhost:8080/status

# Test SSE endpoint
curl -N http://localhost:8080/sse
```

## Next Steps

- Configure monitoring and alerting for production deployments
- Set up log aggregation and analysis
- Implement backup and recovery procedures
- Review [Configuration Guide](configuration.md) for setup guidance
- Check [Troubleshooting Guide](troubleshooting.md) for common issues
