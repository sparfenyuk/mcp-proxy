# MCP-Proxy Headers Issue Analysis and Fix

## Problem Summary

Users report that headers (specifically Authorization headers) are not being passed through to MCP servers when using mcp-proxy, even though the command line parsing appears correct.

## Root Cause Analysis

After investigating the issue reported in [mcp-proxy issue #53](https://github.com/sparfenyuk/mcp-proxy/issues/53), I found several contributing factors:

### 1. MCP-Proxy Code is Correct ✅

The mcp-proxy header handling logic is working correctly:
- Command line argument parsing works properly (`--headers Authorization xyz`)
- Headers are correctly converted to dict format
- Headers are properly passed to both SSE and StreamableHTTP clients

### 2. Known Issues with MCP Python SDK ❌

Based on research of the MCP Python SDK repository, there are several known issues:

#### Issue A: Type Inconsistencies ([MCP SDK Issue #936](https://github.com/modelcontextprotocol/python-sdk/issues/936))
- `sse_client` expects `timeout` as `float`
- `streamablehttp_client` expects `timeout` as `timedelta`
- This causes runtime errors and may affect header handling

#### Issue B: StreamableHTTP Transport Problems
- Multiple users report session termination issues with StreamableHTTP transport
- OpenAI clients specifically fail with StreamableHTTP when using headers
- Premature DELETE operations cause session termination

#### Issue C: SDK Version Issues
- mcp-proxy uses MCP SDK 1.8.0
- Newer versions (1.9.3+) have reported additional issues
- Header handling may have regressions in certain versions

## Solution Implementation

### 1. Upgrade MCP SDK and Add Error Handling

Update `pyproject.toml` to use a more recent stable version and add better error handling.

### 2. Add Header Validation and Logging

Improve debugging by adding header validation and detailed logging.

### 3. Add Compatibility Workarounds

Implement workarounds for known MCP SDK issues.

## Files to be Modified

1. `pyproject.toml` - Update MCP SDK version
2. `src/mcp_proxy/__main__.py` - Add header validation and logging
3. `src/mcp_proxy/sse_client.py` - Add error handling
4. `src/mcp_proxy/streamablehttp_client.py` - Add error handling and workarounds

## Testing

The issue can be reproduced and tested using the provided test script `test_headers_issue.py`.