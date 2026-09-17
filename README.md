# Mercury SE109 Pro — Prometheus Exporter

A small Prometheus exporter for the **Mercury SE109 Pro** (9-port 2.5 Gbps
web-managed switch) and similar TP-Link / FAST / Mercury switches built on the
same Realtek web firmware.

These switches have **no SNMP**. This exporter logs into the web UI, scrapes the
port-statistics and system-info pages, and republishes them as Prometheus
metrics on `/metrics`. Point your own Prometheus at it and graph it in Grafana.

```
Mercury switch ──HTTP scrape──► mercury_exporter :9109/metrics ──► your Prometheus ──► your Grafana
```

- Single Python file, one dependency (`prometheus_client`).
- Runs as a container or a bare process.
- Per-port counters, link state, negotiated speed, device identity, uptime.

---

## Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mercury_switch_up` | gauge | `1` if login + scrape succeeded this cycle, else `0` |
| `mercury_switch_info{hostname,mac,ip,netmask,gateway,firmware,model}` | gauge | Device identity — value is always `1`, data is in the labels |
| `mercury_switch_uptime_seconds` | gauge | Switch uptime in seconds |
| `mercury_port_link_up{port}` | gauge | `1` if the port link is up |
| `mercury_port_admin_enabled{port}` | gauge | `1` if the port is administratively enabled |
| `mercury_port_speed_mbps{port}` | gauge | Negotiated link speed in Mbps (0/10/100/1000/2500/10000) |
| `mercury_port_rx_good_packets_total{port}` | counter | Good packets received |
| `mercury_port_tx_good_packets_total{port}` | counter | Good packets transmitted |
| `mercury_port_rx_bad_packets_total{port}` | counter | Error/bad packets received |
| `mercury_port_tx_bad_packets_total{port}` | counter | Error/bad packets transmitted |

Throughput is derived at query time, e.g. `rate(mercury_port_rx_good_packets_total[5m])`.

> **The switch exposes packet counters, not bytes.** Rates are therefore in
> **packets/sec**, not bits/sec — see [Limitations](#limitations).

---

## Quick start

```bash
git clone https://github.com/YOUR_USER/mercury-se109-exporter.git
cd mercury-se109-exporter
cp .env.example .env
nano .env                      # set SWITCH_HOST, SWITCH_USER, SWITCH_PASS
docker compose up -d --build
```

Metrics are then served at `http://<host>:9109/metrics` — verify with:

```bash
curl -s localhost:9109/metrics | grep '^mercury_'
```

### Scrape it with Prometheus

Add a job to your Prometheus config (full snippet in
[`examples/prometheus-scrape.yml`](examples/prometheus-scrape.yml)):

```yaml
scrape_configs:
  - job_name: mercury_switch
    static_configs:
      - targets: ['EXPORTER_HOST:9109']
```

### Grafana dashboard (optional)

Import [`examples/grafana-dashboard.json`](examples/grafana-dashboard.json) into
your Grafana (Dashboards → Import). It shows device info, uptime, per-port speed,
throughput, and errors. Select your Prometheus data source when prompted.

---

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SWITCH_HOST` | `192.168.0.251` | Switch IP / hostname |
| `SWITCH_USER` | *(required)* | Web UI username |
| `SWITCH_PASS` | *(required)* | Web UI password (plaintext; encoded internally) |
| `EXPORTER_PORT` | `9109` | Port to serve `/metrics` on |
| `SCRAPE_TIMEOUT` | `8` | Per-HTTP-request timeout (seconds) |
| `STATS_PATH` | `PortStatisticsRpm.htm` | Port-statistics page path |

---

## How it works

**1. Authentication.** The web UI does not send the password in clear text — it
obfuscates it client-side with TP-Link's `securityEncode` routine (a
substitution over two fixed keys embedded in the firmware's `cryp_new.js`), then
`POST`s it to `/logon.cgi`. The exporter reproduces that routine exactly
(`security_encode()`), so a plaintext password in `.env` is all you need. On
success the switch returns a `SessionID` cookie, which the exporter reuses and
silently re-authenticates when it expires.

**2. Scraping.** On each Prometheus scrape the exporter fetches two pages:

| Page | Provides |
|------|----------|
| `PortStatisticsRpm.htm` | per-port `state`, `link_status`, and `pkts` arrays |
| `MainRpm.htm` | `info_ds` object (hostname, MAC, IP, firmware, model) + `workTime` (uptime) |

The data is embedded as JavaScript arrays/objects, which the exporter parses
with small regexes — for example `pkts` holds four values per port
(`TxGood, TxBad, RxGood, RxBad`), and `link_status` is a code mapped to a speed:

| code | 0 | 1 | 2/3 | 4/5 | 6 | 7 | 8 |
|------|---|---|-----|-----|---|---|---|
| speed | down | auto | 10M | 100M | 1000M | **2.5G** | **10G** |

(`SPEED_MAP` in the source — confirmed against the SE109 Pro's own `link_info[]`
table; port 9 is the 10G SFP+ uplink.)

**3. Publishing.** Parsed values are exposed through a `prometheus_client`
custom collector, so metrics always reflect a live scrape (no background state).
If a scrape fails, `mercury_switch_up` goes to `0` and the port series are
omitted for that cycle, rather than reporting stale data.

### Adapting to another switch / firmware

Newer or sibling models may lay the JS out slightly differently. Capture the
real page once and adjust:

```bash
docker compose run --rm exporter --dump   # prints the raw stats page HTML
```

Then tweak `parse_stats()` / `SPEED_MAP` (or set `STATS_PATH`) to match.

---

## Limitations

- **Packets, not bytes.** The firmware exposes only packet counters (confirmed
  on both `PortStatisticsRpm.htm` and the detailed `PortStatisticsAllRpm.htm`),
  so accurate **bits/sec** throughput cannot be derived — rates are in
  packets/sec.
- **No CPU / RAM / temperature / fan.** This hardware has no such sensors in its
  web UI. Device-level telemetry is limited to identity + uptime.
- **Speed codes** are verified for the SE109 Pro; other models may differ (see
  "Adapting to another switch" above).

---

## Troubleshooting

**`mercury_switch_up` is `0`** — check the exporter logs: `docker logs mercury-exporter`.

- **`login failed (logonInfo code=1 ...)`** → wrong username/password. Note the
  web password is case-sensitive. Verify by logging into the switch in a browser.
- **`login failed (code=2)`** → account not allowed / temporarily locked.
- **Reads time out, but the host can reach the switch** → **MTU mismatch.** If
  the switch is behind a VPN/cloud tunnel, the path MTU may be below 1500 and
  Docker's bridge (1500) black-holes full-size responses (TCP connects, reads
  hang). Match the host MTU in `docker-compose.yml` (the `networks` block has
  instructions). On a normal LAN this doesn't apply.

**No data in Grafana** — confirm the Prometheus target is `UP`
(`Status → Targets`) and that `mercury_switch_up` exists in Prometheus first.

---

## Security notes

- `.env` holds the switch password and is git-ignored — keep it `chmod 600`.
- The exporter talks **plain HTTP** to the switch (the firmware has no HTTPS).
  Keep the management interface on a trusted VLAN.
- `/metrics` is unauthenticated; restrict it to your monitoring network.

---

## Compatibility

Developed and tested against a **Mercury SE109 Pro**, firmware
`1.0.0 Build 20240914 Rel.40108`. It should work, possibly with small
`SPEED_MAP`/`STATS_PATH` tweaks, on other Mercury / TP-Link / FAST web-managed
switches that use the same Realtek web UI (the `securityEncode` login and
`PortStatisticsRpm.htm` page).

## License

MIT
