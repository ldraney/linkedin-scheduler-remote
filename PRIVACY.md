# Privacy Policy

**linkedin-scheduler-remote** — Last updated: 2026-02-15

## What This Service Does

This is a remote MCP (Model Context Protocol) server that wraps linkedin-mcp-scheduler with LinkedIn OAuth 2.0 authentication over Streamable HTTP transport. It acts as a proxy between AI assistants (e.g., Claude) and the LinkedIn API.

## Data We Handle

### OAuth Tokens
- We store LinkedIn OAuth 2.0 access tokens and refresh tokens locally on the server where this software is deployed.
- Tokens are encrypted at rest using Fernet symmetric encryption.
- Tokens are used solely to authenticate LinkedIn API requests on your behalf.
- Tokens are never shared with third parties.

### LinkedIn Data
- We access your LinkedIn profile information (name, email) solely for authentication purposes.
- We send post content to the LinkedIn API only when you explicitly request it through MCP tool calls.
- We do not store, log, or analyze the content of your LinkedIn posts beyond what is needed for the scheduling queue.

### No Analytics or Tracking
- This software does not include any analytics, telemetry, or tracking.
- No data is sent to any third party beyond LinkedIn's own API.

## Data Storage

- All data is stored locally on the server where this software is deployed.
- The operator of the server is responsible for securing the deployment environment.
- No data is stored in cloud services by this software itself.

## Your Rights

- You can revoke access at any time through your LinkedIn account settings (Settings → Data privacy → Third-party connections).
- Revoking access immediately invalidates all stored tokens.

## Self-Hosted Nature

This is open-source, self-hosted software. The privacy practices depend on how the operator deploys and manages the server. This policy covers the software's behavior — the operator is responsible for their own infrastructure security.

## Contact

For questions about this software's privacy practices, please open an issue at https://github.com/ldraney/linkedin-scheduler-remote/issues.
