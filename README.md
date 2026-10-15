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

Copy `deploy/nginx.conf.example` into your HTTPS `server` block and replace
`CHANGE-ME` with a long random string. Because the proxy accepts any caller's key,
the unguessable prefix is what keeps strangers from using your server as a relay.

## Client usage

    export OPENAI_BASE_URL=https://<your-domain>/openai-<random>/v1
    export OPENAI_API_KEY=sk-...

Smoke test:

    curl https://<your-domain>/openai-<random>/v1/models \
      -H "Authorization: Bearer $OPENAI_API_KEY"

## Tests

    venv/bin/pytest
