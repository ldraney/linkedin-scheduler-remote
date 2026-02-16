[![PyPI](https://img.shields.io/pypi/v/linkedin-scheduler-remote-ldraney)](https://pypi.org/project/linkedin-scheduler-remote-ldraney/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

# linkedin-scheduler-remote

Remote MCP server wrapping [linkedin-mcp-scheduler](https://github.com/ldraney/linkedin-mcp-scheduler) with LinkedIn OAuth 2.0 and Streamable HTTP transport — designed for Claude.ai connectors.

## How it works

```
Claude.ai ──HTTP+Bearer──> linkedin-scheduler-remote ──LinkedIn API──> LinkedIn
                                      │
                                imports linkedin-mcp-scheduler's FastMCP (all 8 tools)
                                patches get_client() with per-request ContextVar
                                adds LinkedIn OAuth + /health + /oauth/callback
```

Three-party OAuth: Claude <-> this server <-> LinkedIn. Each user authorizes their own LinkedIn account via OAuth. The server stores per-user LinkedIn access tokens (encrypted at rest).

## Prerequisites

1. A **LinkedIn Developer App** at [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps):
   - Create an app (or use existing)
   - Under Auth settings, add authorized redirect URL: `{BASE_URL}/oauth/callback`
   - Note the Client ID and Client Secret
   - Ensure the app has the products: "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect"
2. Scopes required: `openid`, `profile`, `email`, `w_member_social`

## Setup

```bash
git clone https://github.com/ldraney/linkedin-scheduler-remote.git
cd linkedin-scheduler-remote
cp .env.example .env
# Edit .env with your LinkedIn OAuth credentials, BASE_URL, and SESSION_SECRET
make install
make run
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `LINKEDIN_OAUTH_CLIENT_ID` | LinkedIn app Client ID |
| `LINKEDIN_OAUTH_CLIENT_SECRET` | LinkedIn app Client Secret |
| `SESSION_SECRET` | Random secret for encrypting token store |
| `BASE_URL` | Public HTTPS URL where this server is reachable |
| `HOST` | Bind address (default: `127.0.0.1`) |
| `PORT` | Listen port (default: `8002`) |

## Verification

```bash
curl http://127.0.0.1:8002/health                                    # {"status": "ok"}
curl http://127.0.0.1:8002/.well-known/oauth-authorization-server    # OAuth metadata
curl -X POST http://127.0.0.1:8002/mcp                               # 401 (auth required)
```

## Tools (8)

All scheduling tools from linkedin-mcp-scheduler are exposed:

- **schedule_post** — Schedule a LinkedIn post for future publication
- **list_scheduled_posts** — List posts with optional status filter
- **get_scheduled_post** — Get details of a scheduled post
- **cancel_scheduled_post** — Cancel a pending post
- **update_scheduled_post** — Edit pending post fields
- **reschedule_post** — Change the scheduled time
- **retry_failed_post** — Reset failed posts to pending
- **queue_summary** — Get queue statistics

## Deploying

### Standalone

Use any HTTPS tunnel (Tailscale Funnel, ngrok, Cloudflare Tunnel) to expose the server publicly, then add the URL as a Claude.ai connector.

A systemd service file is provided in `systemd/linkedin-scheduler-mcp-remote.service`.

### Kubernetes (production)

Production deployment is managed by [mcp-gateway-k8s](https://github.com/ldraney/mcp-gateway-k8s), which runs this server as a pod with Tailscale Funnel ingress.

## Design Notes

The scheduler-remote differs from gcal/notion remotes in one key way: there's a **daemon** (`linkedin-mcp-scheduler-daemon`) that publishes posts to LinkedIn. Currently operates in single-user mode — the daemon uses its own env-var credentials. Multi-user daemon support (per-user credentials stored with each post) is a future enhancement.

## Privacy

See [PRIVACY.md](PRIVACY.md) for our privacy policy.

## License

MIT
