# New-API Channel Proxy Updater

A worker that can setup a cron job to periodically update the proxy IP for specific channels.

The proxy source: https://github.com/TopChina/proxy-list/blob/main/README.md

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/BHznJNs/new-api-proxy-updater)

## Environment Variables

| Variable Name | Description | Example |
|    ---        | ---         | ---     |
| `BASE_URL`    | The URL of your New-API instance | https://new-api.com |
| `ADMIN_ID`    | The ID of your admin user account | `1` |
| `ADMIN_TOKEN` | The access token of your admin user account | `123456`
| `CHANNEL_IDS` | The id of channels that is needed to update proxy IP | `[1, 2, 3, 4]` |
