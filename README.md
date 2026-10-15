# openai-proxy

A thin pass-through proxy that forwards OpenAI API requests (including streaming)
to `https://api.openai.com`. Deploy it on a server that can reach OpenAI, put nginx
(with HTTPS) in front, and point your coding agent's base URL at it.

The client keeps its own OpenAI key and sends it in the `Authorization` header; the
proxy forwards it untouched and stores no secrets. It never logs headers or bodies.

## Run

    ./start.sh     # creates venv, installs deps, runs in background (proxy.log)
    ./stop.sh

Or with Docker:

    docker build -t openai-proxy .
    docker run -d -p 127.0.0.1:8080:8080 --restart unless-stopped openai-proxy

## Configuration (`.env`, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `127.0.0.1` | bind address |
| `PORT` | `8080` | bind port |
| `UPSTREAM_BASE_URL` | `https://api.openai.com` | where requests are forwarded |

## nginx

Copy `deploy/nginx.conf.example` into your HTTPS `server` block. It serves the proxy
at `/openai-proxy/`. Note that the proxy accepts any caller's OpenAI key, so anyone who
finds that path can relay requests through your server (this costs you bandwidth, not
money, and never exposes your key). If that becomes a problem, add an access token check
or switch to an unguessable path.

## Client usage

    export OPENAI_BASE_URL=https://lijialab.com/openai-proxy/v1
    export OPENAI_API_KEY=sk-...

Smoke test:

    curl https://lijialab.com/openai-proxy/v1/models \
      -H "Authorization: Bearer $OPENAI_API_KEY"

### opencode

Add to `~/.config/opencode/opencode.json` (or `opencode.json` in a project root):

    {
      "$schema": "https://opencode.ai/config.json",
      "provider": {
        "openai": {
          "options": {
            "baseURL": "https://lijialab.com/openai-proxy/v1",
            "apiKey": "{env:OPENAI_API_KEY}"
          }
        }
      }
    }

Export `OPENAI_API_KEY` in your shell (or run `opencode auth login`). This works with
API keys only; ChatGPT account login uses a different endpoint the proxy does not cover.
`baseURL` must end in `/v1` with no trailing slash. If replies arrive all at once instead
of streaming, nginx is buffering: add `gzip off;` to the location block.

## Tests

    venv/bin/pytest
