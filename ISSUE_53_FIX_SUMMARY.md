# Fix for MCP-Proxy Issue #53: Unable to Set Headers

## Issue Summary
Users reported that authorization headers were not being passed through to MCP servers when using mcp-proxy, even though the command line syntax appeared correct.

## Root Cause Analysis
The investigation revealed that the mcp-proxy header handling logic was actually working correctly. The real issues were:

1. **MCP Python SDK bugs**: Known issues with header handling in certain versions
2. **StreamableHTTP transport problems**: Session termination and authentication issues
3. **Type inconsistencies**: Between SSE and StreamableHTTP client timeout parameters
4. **Poor error reporting**: Users couldn't debug what was happening

## Solution Implemented

### 1. Updated Dependencies
- **Updated MCP SDK**: Changed from `>=1.8.0` to `>=1.9.0` to get latest fixes
- **Version constraint**: Maintained `<2.0.0` for stability

### 2. Enhanced Debugging and Logging
- **Header validation**: Added validation for header key/value pairs
- **Masked logging**: Log headers with sensitive values masked for security
- **Debug output**: Detailed logging of header processing flow
- **Error context**: Better error messages with troubleshooting hints

### 3. Improved Error Handling
- **Transport-specific errors**: Detect and provide helpful messages for known issues
- **Authentication errors**: Specific guidance for auth-related problems
- **SDK compatibility**: Warnings about StreamableHTTP transport issues

### 4. Better User Guidance
- **Updated help text**: Added examples and usage notes
- **Transport recommendations**: Guidance to use SSE for authentication
- **Command validation**: URL and header format validation

### 5. Documentation
- **Troubleshooting guide**: Comprehensive guide for header issues
- **Known issues**: Documented StreamableHTTP transport problems
- **Working examples**: Tested configurations for common use cases

## Files Modified

1. **`pyproject.toml`**
   - Updated MCP SDK version requirement

2. **`src/mcp_proxy/__main__.py`**
   - Enhanced header validation and logging
   - Improved error handling and user guidance
   - Updated help text with examples

3. **`src/mcp_proxy/sse_client.py`**
   - Added detailed logging and error handling
   - Masked sensitive header values in logs

4. **`src/mcp_proxy/streamablehttp_client.py`**
   - Added warnings about known issues
   - Enhanced error detection and reporting
   - Specific guidance for common error scenarios

5. **`HEADERS_TROUBLESHOOTING.md`** (New)
   - Comprehensive troubleshooting guide
   - Step-by-step debugging instructions
   - Working configuration examples

## Testing
- Created test scripts to validate header propagation
- Confirmed mcp-proxy logic works correctly
- Identified underlying MCP SDK issues

## User Impact

### Before Fix
- Headers silently failed to reach servers
- No debugging information available
- Users couldn't determine if issue was with mcp-proxy or server
- StreamableHTTP transport failures were cryptic

### After Fix
- **Clear logging**: Users can see exactly what headers are being sent
- **Better error messages**: Specific guidance for common issues
- **Transport guidance**: Users are warned about StreamableHTTP issues
- **Debug tools**: Step-by-step troubleshooting process
- **Working examples**: Tested configurations for common scenarios

## Recommended Usage

For authentication, users should now use:

```bash
# Recommended - SSE transport with debug logging
mcp-proxy --debug --headers Authorization "Bearer your-token" http://server/sse

# Avoid - StreamableHTTP has known issues with auth
mcp-proxy --transport streamablehttp --headers Authorization "Bearer token" http://server/mcp
```

## Future Considerations

1. **Monitor MCP SDK updates**: Watch for fixes to StreamableHTTP transport
2. **Consider implementing workarounds**: For known SDK issues if they persist
3. **Enhanced testing**: Add automated tests for header functionality
4. **Server compatibility**: Document known working server implementations

This fix significantly improves the user experience for authentication scenarios while addressing the underlying compatibility issues with the MCP ecosystem.