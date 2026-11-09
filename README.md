# Oasis Monitor

Collect Oasis node status for Telegraf, InfluxDB and Grafana. The collector
reads the node's local control socket as the **node's own service account**;
Telegraf reads a public metrics file and needs neither access to the socket nor
sudo. The sample dashboard uses InfluxQL.

## What is supported

* Oasis Core status and local staking commands tested against **26.1 on Mainnet**
  and **26.4 on Testnet**. Current Core marks the local
  `stake` commands deprecated. If they disappear, node status remains available
  and stake fields are omitted with a warning. Other Core versions need testing.
* Python 3.8+, Linux, systemd, Telegraf 1.x (`inputs.file`, Influx parser),
  InfluxDB 1.x with InfluxQL and Grafana with an InfluxQL data source.
  Telegraf **1.35.3** was tested against live node output; **1.37.0** parsed
  the example configuration locally. InfluxDB and
  Grafana were not available for an end-to-end test of this revision.
  InfluxDB 2.x requires an appropriate v1-compatible DBRP mapping and InfluxQL
  configuration or a separately adapted dashboard; it has not been tested here.
* One systemd instance per node/socket. The normal refresh interval is about
  30 seconds after the preceding collection ends. Each data point retains its
  collection timestamp, so a stopped timer does not make old data look fresh.

The collector emits `oasismonitor` with the existing `entity` and `identityNode`
tags and, when available, the legacy fields for version, peers, consensus
height/epoch, validator status, registration, active stake, self stake and
commission. Set a runtime ID for the legacy runtime fields if the node has more
than one runtime. New `oasis_runtime` points expose rounds, height and executor
roles **per runtime**. `oasis_monitor_health,node=<instance> up=1` means the
status request and required identity succeeded; `up=0` means they failed.
A stopped timer produces no new points: alert on missing data as well as `up=0`.

Values unavailable from the node are **omitted**, not replaced by zero. An empty
or unavailable optional stake/price result does not make a healthy node appear
unhealthy. A runtime's empty executor role list can legitimately mean the node
was not selected in this epoch. `ready` and a growing consensus height alone do
not prove that a ParaTime processes transactions.

## Permissions and trust

The Oasis `internal.sock` is a control interface, not a metrics-only endpoint.
Giving Telegraf access to the socket, its owner's account, or a general sudo rule
can grant more node control than monitoring needs. Do not grant Telegraf
passwordless root, membership in privileged groups, access to node keys, or
permission to alter the collector, configuration, binaries or service units.

The included systemd service runs the collector as `oasis`, the example node
owner. It writes an atomic, world-readable metrics file in a dedicated `/run`
directory; node IDs, validator status and stake are visible to local users. Run
one instance for each node owner and socket, setting `User=` separately if they
differ. Do not run the collector as root. The service account must already be
able to execute the node binary and connect to its socket; directory traversal
and SELinux/AppArmor policy can also affect access. Do not loosen socket or
node-home permissions solely to make Telegraf work.

Existing installations should audit prior access before deployment:

```bash
sudo -l -U telegraf
id telegraf
```

Review the relevant sudoers files with `visudo`, remove obsolete broad rules
and unnecessary privileged group memberships only after checking what else uses
that access, and restart Telegraf after changing its groups. Removing a command
from an old README does not remove an existing sudoers rule. Keep service units,
collector and environment files root-owned and not writable by Telegraf.

## Install

Install Oasis Core, Python 3, systemd and Telegraf using your distribution's
current supported packages and the vendors' instructions. Install the matching
InfluxDB and Grafana versions separately:
[Oasis node setup](https://docs.oasis.io/node/),
[Telegraf installation](https://docs.influxdata.com/telegraf/v1/install/),
[Grafana InfluxDB data source](https://grafana.com/docs/grafana/latest/datasources/influxdb/).

On the node host, from a reviewed checkout:

```bash
git clone https://github.com/rustemar/oasismonitor.git
cd oasismonitor
sudo install -d -o root -g root -m 0755 /usr/local/libexec/oasismonitor /etc/oasismonitor
sudo install -o root -g root -m 0755 metrics-collector.sh metrics_collector.py /usr/local/libexec/oasismonitor/
sudo install -o root -g root -m 0644 systemd/oasismonitor@.service systemd/oasismonitor@.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 telegraf/oasismonitor.conf /etc/telegraf/telegraf.d/oasismonitor.conf
```

Create `/etc/oasismonitor/mainnet.env`, root-owned, mode `0644` (no secrets):

```ini
OASIS_NODE=/absolute/path/to/oasis-node
OASIS_SOCKET=unix:/absolute/path/to/node/internal.sock
OASIS_MONITOR_NAME=mainnet
# Optional, when registration/identity is absent during startup:
# OASIS_ENTITY_ID=<entity-public-key>
# OASIS_NODE_ID=<node-public-key>
# Optional: set to one full runtime ID for legacy dashboard runtime fields.
# OASIS_RUNTIME_ID=0000000000000000000000000000000000000000000000000000000000000000
# Optional: disable deprecated local stake queries if unsupported or unwanted.
# OASIS_STAKE=false
# Optional: HTTPS request to CoinGecko; exposes the host IP to that service.
# OASIS_PRICE=coingecko
```

Set `User=oasis` in the example service to the actual owner using a root-owned
systemd drop-in, for example `sudo systemctl edit oasismonitor@mainnet.service`:

```ini
[Service]
User=your-node-service-user
```

Keep the defaults only if `oasis` is the actual node user. For another node,
create a separate `<name>.env`, matching service instance and Telegraf input
path `/run/oasismonitor-<name>/metrics.lp`. Telegraf must include files from
`/etc/telegraf/telegraf.d`; do not overwrite a working global configuration.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oasismonitor@mainnet.timer
sudo systemctl start oasismonitor@mainnet.service
sudo systemctl status oasismonitor@mainnet.service oasismonitor@mainnet.timer
```

`inputs.file` is used instead of `inputs.exec`: Telegraf only reads the output
file and does not execute code as the node owner. If a particular deployment
already provides an intentionally restricted metrics-only interface to Telegraf,
it may be possible to run `metrics-collector.sh` directly via `inputs.exec`
without sudo, provided the proxy supports the required RPCs; this configuration
has not been tested. A control socket shared with Telegraf is **not** a
metrics-only interface. For
`inputs.exec`, set `data_format = "influx"` and an adequate timeout.

## InfluxDB and Grafana

Configure Telegraf's output separately in a root-controlled configuration.
For example, `telegraf/influxdb-v2.conf.example` uses a TLS endpoint and a
write-scoped token supplied from a root-only systemd `EnvironmentFile`. Install
the example as a `.conf` file only after replacing the endpoint, organization
and bucket. Example `/etc/systemd/system/telegraf.service.d/oasis-secrets.conf`:

```ini
[Service]
EnvironmentFile=/etc/telegraf/oasis-secrets.env
```

Create that environment file as root with mode `0600` and a line
`INFLUX_TOKEN=<your-token>`, using a protected editor. Reload systemd and restart
Telegraf after setting the file. The token is passed in the Telegraf process
environment; restrict local administrative access accordingly. Do not put it in
the command line. The example is a template and has not been tested with a live
InfluxDB server.

Use a dedicated write-only or least-privilege credential, TLS with certificate
verification for network traffic, and network/firewall restrictions. On a
single host use a protected local connection if appropriate. Avoid placing
credentials in this repository, shell history, process arguments, public logs or
the node environment file. Protect Telegraf's output configuration and any
credential files from unprivileged readers. Configure InfluxDB 1.x with
`outputs.influxdb`, or InfluxDB 2.x with `outputs.influxdb_v2`; credentials,
bucket/database and access policies depend on your deployment.

Import `oasis-community-dashboard.json` in Grafana. The panels use the default
data source: make an InfluxQL data source with access to the measurements the
default, or assign it to the panels and the entity variable after import. The entity selector comes from data
already stored in the database. The included dashboard keeps the established
measurement/tag/field names where meaningful. The legacy «Current reward» panel
was removed: change in self stake is affected by deposits, withdrawals and
commission, so it is not a reliable profit calculation. The obsolete storage
roles query was removed. The price panel is empty unless `OASIS_PRICE=coingecko`;
it is an approximate USD price, whereas the old script used Binance ROSE/USDT.
Historic prices and current prices therefore do not have precisely the same
meaning. Historical `currentReward` values remain in the database but the new
collector never writes that field. Missing or unsupported runtime/stake fields
will leave corresponding older panels empty; old database fields are not deleted.
InfluxDB field types are preserved as floats for legacy numeric fields.

## Check and troubleshoot

```bash
sudo journalctl -u oasismonitor@mainnet.service -n 50 --no-pager
sudo -u telegraf cat /run/oasismonitor-mainnet/metrics.lp
sudo -u telegraf telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d --test
sudo systemctl restart telegraf
```

A first read before the service has produced its file fails; check the service
journal and retry. If the node is unavailable, the file is replaced by `up=0`
and no fabricated height/stake fields. If Telegraf cannot read the file, check
the runtime directory and file permissions; do not grant it socket access. If
stake fields are missing, inspect the service journal: the Core staking CLI is
deprecated and can fail independently. `OASIS_RUNTIME_ID` must exactly match an
ID in `oasis-node control status`; when absent and multiple runtimes exist,
per-runtime metrics are still emitted but legacy runtime fields are omitted.
External price errors do not prevent local metrics.

For an old installation, remove its old `inputs.exec` entry and any old
collector timer only after verifying the new instance and removing obsolete
privileges. Avoid sending both collectors to the same series: otherwise values
and timestamps can conflict. Review dashboard selection and alerts for missing
data after migration. An equivalent transient systemd service with the same sandbox options was tested
on the Testnet node; the shipped service and timer have not been installed as a
production deployment. A running InfluxDB/Grafana pipeline was not tested.

Run the offline checks with `python3 -m unittest discover -s tests -v`.
