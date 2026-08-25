# plausible-mcp

An MCP server that lets everyone in an organisation query
[Plausible Analytics](https://plausible.io) from Claude.

One deployment serves the whole org. It holds a single Plausible API key server-side
and authenticates each person with Google, so nobody needs their own key and nobody
has to be granted access individually: put your Google Workspace domain in
`ALLOWED_DOMAINS` and anyone with an address there can connect the first time they
try. Colleagues outside that domain go in `ALLOWED_EMAILS`, one address at a time.

If you run analytics for a team and want them reading the numbers themselves rather
than asking you for them, that is what this is for.

## How authentication works

Claude registers itself with this server using OAuth 2.1 dynamic client registration.
Google does not support dynamic client registration, so this server acts as its own
authorization server and delegates the human login upstream to Google.

Google establishes who the person is. It does not decide whether they may read your
analytics. A middleware checks each caller's verified email address against
`ALLOWED_DOMAINS` and `ALLOWED_EMAILS` on every tool call. If you set neither, the
server refuses to start.

`ALLOWED_DOMAINS` is the part that makes this org-wide: it matches the domain of the
verified address exactly, so `example.com` admits `alice@example.com` but not
`alice@evil-example.com` or `alice@example.com.evil.com`.

To revoke someone, take them out of `ALLOWED_EMAILS` or `ALLOWED_DOMAINS` and
redeploy. Suspending their Google account stops them signing in again, but the
allowlist reads the claims from a token this server signed, so one already issued
keeps working until it expires. Rotating `JWT_SIGNING_KEY` invalidates every token
at once, at the cost of signing everybody out.

## Tools

- `query` runs a Stats API v2 query. Grouping by different dimensions gives you
  timeseries, breakdowns and goal conversions from the same tool. To compare two
  periods, call it once per period.
- `list_sites` returns the site domains you can pass as `site_id`.
- `usage_stats` reports who has used the server, how often, and which accounts were
  refused.

## Usage tracking

The allowlist middleware sees every caller's email address, because it has to check
it. It records a per-person call count, a first-seen and last-seen time, and a
per-tool breakdown to the volume at `USAGE_PATH`. Refused accounts are counted
separately, which is how you spot someone outside the allowlist trying to connect.

Read it with the `usage_stats` tool. Anyone on the allowlist can see everyone's
rows. Unset `USAGE_PATH` to turn tracking off.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | yes | Google OAuth client used to sign people in |
| `PLAUSIBLE_API_KEY` | yes | Plausible key with Stats API access; shared by everyone |
| `BASE_URL` | yes | Public URL of this server; the Google redirect URI is `BASE_URL` + `/auth/callback` |
| `ALLOWED_DOMAINS` / `ALLOWED_EMAILS` | one of them | Who may query. Neither set means the server refuses to start |
| `JWT_SIGNING_KEY` | recommended | Signs this server's access tokens. Without it a restart signs everyone out |
| `CLIENT_STORAGE_PATH` | no | Where to persist OAuth client registrations. Unset means in-memory |
| `USAGE_PATH` | no | Where to record per-person usage. Unset turns tracking off |

See `.env.example` for the same list in copy-pasteable form.

## Deploy

The repo ships a `fly.toml`, so the instructions below are for [Fly.io](https://fly.io),
but nothing in the server is Fly-specific: it is one container that wants a writable
directory and the environment above. Replace `YOUR-APP` throughout.

1. Create an OAuth client in the Google Cloud console. Choose **Web application** and
   set the authorized redirect URI to `https://YOUR-APP.fly.dev/auth/callback`.
   The URI must match exactly.

2. Create the app and its volume:

   ```bash
   fly apps create YOUR-APP --org YOUR-ORG
   fly volumes create plausible_mcp_data --app YOUR-APP --region fra --size 1 --yes
   ```

   Use `fly apps create` rather than `fly launch`, which regenerates `fly.toml`
   and would discard the volume mount and health check.

3. Point `app` in `fly.toml` at your app name.

4. Set the configuration. `BASE_URL` and `ALLOWED_DOMAINS` go here rather than in
   `fly.toml` because they differ per deployment, and a placeholder committed to the
   repo would overwrite the real value on the next deploy:

   ```bash
   fly secrets set --app YOUR-APP \
     BASE_URL=https://YOUR-APP.fly.dev \
     ALLOWED_DOMAINS=your-domain.org \
     GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com \
     GOOGLE_CLIENT_SECRET=GOCSPX-xxx \
     PLAUSIBLE_API_KEY=xxx \
     JWT_SIGNING_KEY="$(openssl rand -hex 32)"
   ```

   Keep `JWT_SIGNING_KEY` stable: changing it signs everyone out.

5. `fly deploy --ha=false`

   Without `--ha=false` Fly may start two machines. Each would get its own volume,
   so a sign-in that registers on one machine fails when the next request lands on
   the other.

## Add it to Claude

To add it for a whole team, go to **Settings > Connectors** in claude.ai, choose
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

## Licence

MIT — see [LICENSE](LICENSE).
