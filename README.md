# linkedin-scheduler-remote

HTTP transport wrapper for [linkedin-mcp-scheduler](https://github.com/ldraney/linkedin-mcp-scheduler), following the pattern of:

- [gcl-mcp-remote](https://github.com/ldraney/gcl-mcp-remote)
- [notion-mcp-remote](https://github.com/ldraney/notion-mcp-remote)

Wraps the linkedin-mcp-scheduler MCP server with HTTP/SSE transport so it can be accessed over the network — from containers, remote agents, or any MCP client that speaks HTTP.

## Status

Early stage — will be built out after linkedin-mcp-scheduler core is stable.
