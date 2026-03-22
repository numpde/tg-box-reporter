# tg-box-reporter

`tg-box-reporter` is a generic two-process reporter:

- a local `collector` reads host and container state and serves a sanitized JSON snapshot over HTTP
- a separate `relay` fetches that snapshot and sends formatted reports to Telegram

The split keeps Docker and host access on the box-local collector. The Telegram relay only needs a collector URL and bot credentials.

## What it reports

- hostname
- uptime
- load average
- memory and swap usage
- disk usage
- Docker container summary
- top containers by CPU and memory percentage
- restart counts and health state when available
- threshold-driven host and container problem detection

## Why split collector and relay

- the collector can stay on the monitored host with Docker access
- the relay does not need Docker socket access
- multiple relays could reuse the same collector
- the relay can run locally or remotely if it can reach the collector endpoint

## Quick Start

Create a virtual environment if you want one, then:

```bash
python3 -m tg_box_reporter.collector
```

In another shell:

```bash
export TG_BOT_TOKEN=...
export TG_CHAT_ID=...
python3 -m tg_box_reporter.relay
```

Defaults:

- collector bind: `127.0.0.1:9707`
- collector snapshot URL: `http://127.0.0.1:9707/snapshot`
- relay mode: `hybrid`

Collector endpoints:

- `/healthz`
- `/readyz`
- `/snapshot`
- `/summary`
- `/containers`
- `/problems`
- `/events`
- `/events/recent`
- `/events/summary`

## Environment

See `.env.example` for the full contract.

Collector:

- `COLLECTOR_BIND_HOST`
- `COLLECTOR_PORT`
- `COLLECTOR_CACHE_SECONDS`
- `COLLECTOR_HOST_PROC`
- `COLLECTOR_HOST_ROOT`
- `COLLECTOR_DISK_PATH`
- `COLLECTOR_DOCKER_BIN`
- `COLLECTOR_INCLUDE_STOPPED`
- `COLLECTOR_NAME_INCLUDE_REGEX`
- `COLLECTOR_NAME_EXCLUDE_REGEX`
- `COLLECTOR_REQUIRE_DOCKER`
- `COLLECTOR_ALERT_LOAD_PER_CPU_GT`
- `COLLECTOR_ALERT_MEM_PERCENT_GT`
- `COLLECTOR_ALERT_SWAP_USED_MB_GT`
- `COLLECTOR_ALERT_DISK_PERCENT_GT`
- `COLLECTOR_ALERT_CONTAINER_RESTART_COUNT_GT`
- `COLLECTOR_ALERT_CONTAINER_CPU_PERCENT_GT`
- `COLLECTOR_ALERT_CONTAINER_MEM_PERCENT_GT`
- `COLLECTOR_DOCKER_TIMEOUT_SECONDS`
- `COLLECTOR_EVENT_TOKEN`
- `COLLECTOR_EVENT_MAX_RECENT`
- `COLLECTOR_EVENT_RETENTION_SECONDS`
- `COLLECTOR_EVENT_MAX_BYTES`

Relay:

- `TG_BOT_TOKEN`
- `TG_CHAT_ID`
- `TG_ALLOWED_CHAT_IDS`
- `TG_API_BASE`
- `COLLECTOR_URL`
- `RELAY_MODE`
- `RELAY_INTERVAL_SECONDS`
- `RELAY_STARTUP_REPORT`
- `RELAY_REQUEST_TIMEOUT_SECONDS`
- `TG_GET_UPDATES_TIMEOUT_SECONDS`
- `REPORT_MAX_CONTAINERS`
- `RELAY_HEARTBEAT_PATH`
- `RELAY_HEALTH_STALE_SECONDS`

## Commands

The relay supports:

- `/report`
- `/summary`
- `/containers`
- `/problems`
- `/events`
- `/help`

## Containerized Deployment Notes

If you run the collector in a container and want host metrics, mount host paths into the container and point the env vars at those mounts, for example:

- host `/proc` -> container `/host/proc`
- host `/` -> container `/hostfs`
- host `/var/run/docker.sock` -> container `/var/run/docker.sock`

Then set:

- `COLLECTOR_HOST_PROC=/host/proc`
- `COLLECTOR_HOST_ROOT=/hostfs`
- `COLLECTOR_DISK_PATH=/hostfs`

Important: Docker socket access is still effectively host-level access. The split design keeps that power away from the Telegram relay, but it does not remove the collector's privilege.

## Collector API

`/snapshot` is the full payload. The collector also exposes stable projections so downstream clients do not need to duplicate filtering logic:

- `/summary`: compact host/docker summary plus overall status and problem counts
- `/containers?limit=N`: sorted container list, hottest first
- `/problems`: only detected problems and their counts
- `/events`: combined recent event and event-summary view
- `/events/recent`: recent ingested events
- `/events/summary`: grouped event counts
- `/readyz`: validates that the collector can currently produce a snapshot

Threshold env vars are collector-side SSoT for problem detection. Set them to a negative value to disable that specific check.
`COLLECTOR_DOCKER_TIMEOUT_SECONDS` bounds each Docker CLI call so the collector degrades cleanly instead of hanging forever.
`POST /events` accepts authenticated JSON events when `COLLECTOR_EVENT_TOKEN` is set.
`COLLECTOR_SHARED_NETWORK` and `COLLECTOR_SHARED_ALIAS` are only used by the optional shared-network compose override.

## Health Model

- `/healthz` is process liveness only
- `/readyz` forces a snapshot load and is the right endpoint for deployment healthchecks
- the relay writes a heartbeat file and the compose healthcheck validates that the polling loop is still making progress
- `RELAY_HEALTH_STALE_SECONDS` must stay above the configured long-poll/request timeout window or relay healthchecks will flap by design

## Deployment Artifacts

This repo now includes generic deployment artifacts:

- [Dockerfile](Dockerfile): one image for both collector and relay
- [docker-compose.example.yml](docker-compose.example.yml): sample two-service stack
- [docker-compose.shared-network.example.yml](docker-compose.shared-network.example.yml): optional collector-only attach to an external Docker network for cross-container event ingress
- [collector.env.example](contrib/env/collector.env.example): split collector env file
- [relay.env.example](contrib/env/relay.env.example): split relay env file
- [tg-box-collector.service](contrib/systemd/tg-box-collector.service): host service unit
- [tg-box-relay.service](contrib/systemd/tg-box-relay.service): host service unit

Container stack example:

```bash
TG_BOT_TOKEN=... TG_CHAT_ID=... docker compose -f docker-compose.example.yml up -d --build
```

If other containers need Docker-DNS reachability to the collector for `POST /events`, keep that off the base stack and add the optional override instead:

```bash
docker network create tg-reporting
COLLECTOR_SHARED_NETWORK=tg-reporting \
COLLECTOR_SHARED_ALIAS=tg-box-collector \
TG_BOT_TOKEN=... TG_CHAT_ID=... \
docker compose \
  -f docker-compose.example.yml \
  -f docker-compose.shared-network.example.yml \
  up -d --build
```

That leaves the relay on the private default network while giving emitting app containers a stable collector hostname such as `http://tg-box-collector:9707/events`.

Host service example:

1. Copy the repo to `/opt/tg-box-reporter`.
2. Copy [collector.env.example](contrib/env/collector.env.example) to `/etc/tg-box-reporter/collector.env`.
3. Copy [relay.env.example](contrib/env/relay.env.example) to `/etc/tg-box-reporter/relay.env`.
4. Install the unit files from [contrib/systemd](contrib/systemd).
5. Run `systemctl daemon-reload && systemctl enable --now tg-box-collector tg-box-relay`.

For host deployments, the collector process must run as a user that can talk to Docker if you want container data. The sample units leave that choice to the operator instead of hardcoding a specific local user.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
