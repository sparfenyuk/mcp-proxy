# Troubleshooting Guide

This guide covers common issues you might encounter when using MCP Foxxy Bridge
and how to resolve them.

## Common Issues

### Server Connection Issues

#### MCP servers fail to connect

**Symptoms:**

- Bridge starts but servers show "failed" status
- Error messages about connection timeouts
- Tools are not available

**Possible Causes and Solutions:**

1. **Incorrect command or arguments**

   ```bash
   # Check if the command works directly
   npx -y @modelcontextprotocol/server-github
   ```

   If this fails, fix the command in your configuration.

2. **Missing dependencies**

   ```bash
   # Install missing MCP servers
   npm install -g @modelcontextprotocol/server-github
   npm install -g @modelcontextprotocol/server-filesystem
   
   # Or use npx to auto-install
   npx -y @modelcontextprotocol/server-github
   ```

3. **Environment variables not set**

   ```bash
   # Check required environment variables
   echo $GITHUB_TOKEN
   
   # Set if missing
   export GITHUB_TOKEN=your_token_here
   ```

4. **Permission issues**

   ```bash
   # Check file permissions
   ls -la /path/to/directory
   
   # Fix permissions if needed
   chmod 755 /path/to/directory
   ```

#### Async context manager errors

**Error message:** `"_AsyncGeneratorContextManager can't be used in 'await' expression"`

**Solution:** This is a known issue that has been fixed. Update to the latest
version of MCP Foxxy Bridge:

```bash
uv tool upgrade mcp-foxxy-bridge
```

### Port and Network Issues

#### Port already in use

**Symptoms:**

- Bridge fails to start
- Error about port being in use

**Solution:** The bridge automatically finds available ports. If you need a
specific port, ensure nothing else is using it:

```bash
# Check what's using the port
lsof -i :8080

# Kill the process if needed
kill -9 <PID>

# Or use a different port
mcp-foxxy-bridge --bridge-config config.json --port 8081
```

#### Can't connect to bridge from client

**Symptoms:**

- MCP client can't connect to bridge
- Connection refused or timeout errors

**Solutions:**

1. **Check if bridge is running**

   ```bash
   curl http://localhost:8080/status
   ```

2. **Verify correct endpoint**

   MCP clients should connect to `/sse`:

   ```text
   http://localhost:8080/sse
   ```

3. **Check host binding**

   ```bash
   # For local access only (default)
   mcp-foxxy-bridge --bridge-config config.json --host 127.0.0.1
   
   # For network access
   mcp-foxxy-bridge --bridge-config config.json --host 0.0.0.0
   ```

4. **Firewall issues**

   ```bash
   # Allow port through firewall (Linux)
   sudo ufw allow 8080
   
   # Check firewall rules
   sudo ufw status
   ```

### Configuration Issues

#### Environment variable expansion not working

**Symptoms:**

- Variables like `${GITHUB_TOKEN}` appear literally in logs
- Authentication failures

**Solutions:**

1. **Check variable syntax**

   ```json
   {
     "env": {
       "TOKEN": "${GITHUB_TOKEN}",          // ✅ Correct
       "TOKEN": "$GITHUB_TOKEN",            // ❌ Wrong
       "TOKEN": "${GITHUB_TOKEN:default}"   // ✅ With default
     }
   }
   ```

2. **Verify environment variables are set**

   ```bash
   # Check if variable exists
   printenv | grep GITHUB_TOKEN
   
   # Set if missing
   export GITHUB_TOKEN=your_token_here
   ```

#### Configuration file not found

**Error:** `"Configuration file not found"`

**Solutions:**

1. **Check file path**

   ```bash
   # Use absolute path
   mcp-foxxy-bridge --bridge-config /full/path/to/config.json
   
   # Or relative to current directory
   ls -la config.json
   ```

2. **Check file permissions**

   ```bash
   ls -la config.json
   chmod 644 config.json
   ```

#### Invalid JSON configuration

**Error:** `"Failed to parse configuration"`

**Solutions:**

1. **Validate JSON syntax**

   ```bash
   # Check JSON validity
   python -m json.tool config.json
   
   # Or use jq
   jq . config.json
   ```

2. **Common JSON errors**

   - Missing commas between objects
   - Trailing commas (not allowed in JSON)
   - Unquoted keys or values
   - Mismatched brackets

### Tool and Resource Issues

#### Tools not showing up

**Symptoms:**

- Bridge status shows servers connected
- But `list_tools()` returns empty or partial results

**Solutions:**

1. **Check server status**

   ```bash
   curl -s http://localhost:8080/status | jq '.server_instances'
   ```

2. **Verify server capabilities**

   Test individual servers manually:

   ```bash
   # Test GitHub server directly
   npx -y @modelcontextprotocol/server-github
   ```

3. **Check namespacing configuration**

   ```json
   {
     "mcpServers": {
       "github": {
         "toolNamespace": "github",  // Tools will have 'github.' prefix
         "enabled": true
       }
     }
   }
   ```

#### Tool calls fail

**Error:** `"Tool 'toolname' not found"`

**Solutions:**

1. **Check tool name format**

   With namespacing enabled, use:

   ```text
   github.search_repositories  // ✅ Correct
   search_repositories         // ❌ Missing namespace
   ```

2. **Verify server connection**

   ```bash
   curl -s http://localhost:8080/status | jq '.server_instances.github.status'
   ```

3. **Check server logs**

   Run bridge with debug mode:

   ```bash
   mcp-foxxy-bridge --bridge-config config.json --debug
   ```

### Docker Issues

#### Container fails to start

**Solutions:**

1. **Check Docker build**

   ```bash
   docker build -t mcp-foxxy-bridge .
   ```

2. **Verify volume mounts**

   ```bash
   # Ensure config file exists
   ls -la ./config.json
   
   # Use absolute paths in volume mounts
   docker run -v $(pwd)/config.json:/app/config/config.json:ro ...
   ```

3. **Check environment variables**

   ```bash
   # Pass environment variables correctly
   docker run -e GITHUB_TOKEN=your_token ...
   ```

#### Permission denied in container

**Solutions:**

1. **Fix file permissions**

   ```bash
   chmod 644 config.json
   ```

2. **Check Docker user**

   The container runs as a non-root user. Ensure files are readable.

### Performance Issues

#### High memory usage

**Symptoms:**

- Bridge uses excessive memory
- Out of memory errors

**Solutions:**

1. **Check server health**

   Unhealthy servers may cause memory leaks:

   ```bash
   curl -s http://localhost:8080/status | \\
     jq '.server_instances[] | select(.status != "connected")'
   ```

2. **Reduce server count**

   Disable unused servers:

   ```json
   {
     "mcpServers": {
       "unused_server": {
         "enabled": false
       }
     }
   }
   ```

3. **Adjust timeouts**

   ```json
   {
     "mcpServers": {
       "server_name": {
         "timeout": 30,           // Shorter timeout
         "retryAttempts": 2       // Fewer retries
       }
     }
   }
   ```

#### Slow response times

**Solutions:**

1. **Check individual server performance**

   Test servers directly to identify slow ones.

2. **Optimize configuration**

   ```json
   {
     "bridge": {
       "failover": {
         "enabled": true,
         "maxFailures": 2,        // Fail faster
         "recoveryInterval": 30000
       }
     }
   }
   ```

## Debugging Techniques

### Enable Debug Logging

```bash
mcp-foxxy-bridge --bridge-config config.json --debug
```

This will show:

- Server connection attempts
- Tool routing decisions
- Error details and stack traces

### Check Server Status

```bash
# Get detailed status
curl -s http://localhost:8080/status | python -m json.tool

# Monitor status continuously
watch -n 2 'curl -s http://localhost:8080/status | jq .server_instances'
```

### Test Individual Components

1. **Test MCP servers directly**

   ```bash
   # Run server in isolation
   npx -y @modelcontextprotocol/server-github
   ```

2. **Test bridge connectivity**

   ```bash
   # Check if bridge responds
   curl -v http://localhost:8080/sse
   ```

3. **Test configuration parsing**

   ```bash
   # Validate config syntax
   python -c "import json; json.load(open('config.json'))"
   ```

### Common Debug Patterns

1. **Connection issues**: Check command, args, and environment variables
2. **Tool routing issues**: Verify namespacing and server status
3. **Performance issues**: Check server health and timeout settings
4. **Configuration issues**: Validate JSON and file permissions

## Getting Additional Help

### Collecting Information

When seeking help, include:

1. **Bridge version**

   ```bash
   mcp-foxxy-bridge --version
   ```

2. **Configuration file** (remove sensitive data)

   ```bash
   # Sanitize your config
   cat config.json | sed 's/ghp_[^"]*/"<redacted>"/g'
   ```

3. **Status output**

   ```bash
   curl -s http://localhost:8080/status | jq .
   ```

4. **Debug logs**

   ```bash
   mcp-foxxy-bridge --bridge-config config.json --debug 2>&1 | head -50
   ```

5. **Environment details**

   - Operating system and version
   - Python version (`python --version`)
   - Node.js version (`node --version`)
   - UV version (`uv --version`)

### Where to Get Help

- **GitHub Issues**: <https://github.com/billyjbryant/mcp-foxxy-bridge/issues>
- **Configuration Guide**: [configuration.md](configuration.md)
- **API Reference**: [api.md](api.md)
- **Architecture Overview**: [architecture.md](architecture.md)

### Before Opening an Issue

1. Check existing issues for similar problems
2. Verify you're using the latest version
3. Test with minimal configuration
4. Collect debug information as described above

## FAQ

### Q: Why do I get "port already in use" errors?

**A:** The bridge automatically finds available ports starting from your
requested port. If you see this error, another process is likely using a
range of ports. Try a different port range or kill the conflicting process.

### Q: My tools have weird prefixes like "github.search_repositories"

**A:** This is namespacing to prevent conflicts between servers. You can:

- Use the full namespaced name in tool calls
- Disable namespacing with `"toolNamespace": null`
- Customize the namespace with `"toolNamespace": "custom"`

### Q: Some servers connect but others don't

**A:** Check each server individually:

1. Verify the command works outside the bridge
2. Check environment variables are set
3. Look at debug logs for specific error messages
4. Ensure dependencies are installed

### Q: The bridge works locally but not in Docker

**A:** Common Docker issues:

- File permissions (use `chmod 644` on config files)
- Environment variables not passed correctly
- Volume mounts using relative paths
- Network configuration preventing connections

### Q: Can I use HTTP instead of SSE?

**A:** The bridge supports both SSE and StreamableHTTP transports. Most MCP
clients use SSE by default, but you can use the HTTP endpoint if your client
supports it.
