# MCP-Proxy Headers Troubleshooting Guide

## Issue Description

Users report that authorization headers are not being passed through to MCP servers when using mcp-proxy, particularly when connecting to servers that require authentication.

## Quick Fix Summary

### ✅ What Works
- **SSE Transport with Headers** (Recommended)
  ```bash
  mcp-proxy --headers Authorization "Bearer your-token" http://your-server/sse
  ```

### ⚠️ Known Issues
- **StreamableHTTP Transport with Headers** has known compatibility issues
- Some MCP SDK versions have header handling bugs
- OpenAI client specifically has issues with StreamableHTTP + authentication

## Troubleshooting Steps

### 1. Enable Debug Logging
Always start with debug logging to see what's happening:

```bash
mcp-proxy --debug --headers Authorization "your-token" http://your-server/sse
```

This will show you:
- How headers are being parsed
- What headers are being sent to the MCP server
- Connection details and any errors

### 2. Use SSE Transport (Default)
SSE transport is more stable for authentication:

```bash
# Good - uses SSE (default)
mcp-proxy --headers Authorization "Bearer token" http://server/sse

# Avoid - StreamableHTTP has issues
mcp-proxy --transport streamablehttp --headers Authorization "Bearer token" http://server/mcp
```

### 3. Validate Your Command Line
Make sure your headers are properly formatted:

```bash
# Correct syntax
mcp-proxy --headers Authorization "Bearer your-token" http://server/sse
mcp-proxy -H Authorization "Bearer your-token" http://server/sse

# Multiple headers
mcp-proxy -H Authorization "Bearer token" -H Content-Type "application/json" http://server/sse

# Wrong - missing quotes around value with spaces
mcp-proxy --headers Authorization Bearer your-token http://server/sse
```

### 4. Test with MCP Inspector
Before using mcp-proxy, test your server directly with MCP Inspector to ensure:
1. Your server works with authentication
2. Your token/credentials are correct
3. The server responds properly to authenticated requests

### 5. Check Server-Side Logs
Enable logging on your MCP server to see if headers are being received:
- Check for Authorization header in server logs
- Verify the header value is correct
- Look for authentication errors

## Common Error Messages and Solutions

### "Session terminated"
**Cause**: Known issue with StreamableHTTP transport when using authentication  
**Solution**: Switch to SSE transport (remove `--transport streamablehttp`)

### "TaskGroup exception" or "unhandled errors"
**Cause**: MCP SDK timeout parameter type inconsistencies  
**Solution**: Use SSE transport instead of StreamableHTTP

### "Authorization header not found"
**Cause**: Headers not being properly passed through  
**Solutions**:
1. Use debug logging to trace the issue
2. Check command line syntax
3. Try SSE transport
4. Verify server expects the header format you're sending

### Server returns 401/403 errors
**Cause**: Authentication issue  
**Solutions**:
1. Verify your token/credentials are correct
2. Check if server expects different header format
3. Test with MCP Inspector first
4. Check server documentation for auth requirements

## Environment Variables

You can also set authentication via environment variable:

```bash
export API_ACCESS_TOKEN="your-token"
mcp-proxy http://your-server/sse
```

This will automatically add `Authorization: Bearer your-token` header.

## Testing Your Setup

Use this test command to verify everything works:

```bash
# Test with debug logging
mcp-proxy --debug --headers Authorization "test-token" http://httpbin.org/headers
```

This will show you exactly what headers are being sent.

## Known Working Configurations

### Claude Desktop with mcp-proxy
```json
{
  "mcpServers": {
    "my-server": {
      "command": "mcp-proxy",
      "args": [
        "--headers", "Authorization", "Bearer your-token",
        "http://your-server/sse"
      ]
    }
  }
}
```

### VSCode with mcp-proxy
```json
{
  "mcp-local": {
    "command": "uvx",
    "args": [
      "mcp-proxy",
      "--debug",
      "--headers", "Authorization", "${input:my-api-key}",
      "http://127.0.0.1:8081/sse"
    ]
  }
}
```

## Getting Help

If you're still having issues:

1. **Enable debug logging** and capture the full output
2. **Test with MCP Inspector** to isolate the issue
3. **Check server logs** to see what's being received
4. **Try SSE transport** if using StreamableHTTP
5. **Open an issue** with the debug logs and your configuration

## Recent Fixes

This version includes several improvements:
- Enhanced header validation and logging
- Better error messages for known issues
- Updated MCP SDK version
- Warnings about StreamableHTTP transport issues
- Improved debugging output

## Version Compatibility

- **mcp-proxy 0.8.0+**: Includes header fixes and improved logging
- **MCP SDK 1.9.0+**: Required for latest compatibility
- **SSE transport**: Recommended for all authentication use cases
- **StreamableHTTP transport**: Not recommended for authentication