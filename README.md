# plausible-mcp

An MCP server that lets the team query Plausible Analytics from Claude. It holds a
single Plausible API key server-side and authenticates each person with Google, so
nobody needs their own key.

## How authentication works

Claude registers itself with this server using OAuth 2.1 dynamic client registration.
Google does not support dynamic client registration, so this server acts as its own
authorization server and delegates the human login upstream to Google.

Google establishes who the person is. It does not decide whether they may read your
analytics. A middleware checks each caller's verified email address against
`ALLOWED_DOMAINS` and `ALLOWED_EMAILS` on every tool call. If you set neither, the
server refuses to start.

## Tools

- `query` runs a Stats API v2 query. Grouping by different dimensions gives you
  timeseries, breakdowns and goal conversions from the same tool. To compare two
  periods, call it once per period.
- `list_sites` returns the site domains you can pass as `site_id`.

## Deploy

1. Create an OAuth client in the Google Cloud console. Choose **Web application** and
   set the authorized redirect URI to `https://YOUR-APP.fly.dev/auth/callback`.
   The URI must match exactly.

2. Create the app and its volume:

   ```bash
   fly launch --no-deploy --name plausible-mcp --region fra
   fly volumes create plausible_mcp_data --region fra --size 1
   ```

3. Set the secrets:

   ```bash
   fly secrets set \
     GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com \
     GOOGLE_CLIENT_SECRET=GOCSPX-xxx \
     PLAUSIBLE_API_KEY=xxx \
     ALLOWED_EMAILS=guest@example.net \
     JWT_SIGNING_KEY="$(openssl rand -hex 32)"
   ```

   `BASE_URL` and `ALLOWED_DOMAINS` are already set in `fly.toml`. Keep
   `JWT_SIGNING_KEY` stable: changing it signs everyone out.

4. `fly deploy --ha=false`

   Without `--ha=false` Fly may start two machines. Each would get its own volume,
   so a sign-in that registers on one machine fails when the next request lands on
   the other.

## Add it to Claude

To add it for the whole team, go to **Settings > Connectors** in claude.ai, choose
**Add custom connector**, and enter `https://YOUR-APP.fly.dev/mcp`. Each person
signs in with Google the first time they use it.

To add it to Claude Code:

```bash
claude mcp add --transport http plausible https://YOUR-APP.fly.dev/mcp
```

## Run it locally

```bash
uv venv && uv pip install -r pyproject.toml
cp .env.example .env    # then fill it in
set -a && source .env && set +a
python server.py
```

Add `http://localhost:8000/auth/callback` to the Google client's redirect URIs to test
the sign-in flow locally.

Run the tests with `python test_server.py`. To exercise the OAuth flow and the tools by
hand, use `npx @modelcontextprotocol/inspector`.
