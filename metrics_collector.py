#!/usr/bin/env python3
"""Read Oasis status and emit Influx line protocol; no shell or sudo needed."""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

TIMEOUT = 7


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, check=True)
    return result.stdout


def tag(value):
    return str(value).replace('\n', ' ').replace('\r', ' ').replace('\\', '\\\\').replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')


def field_string(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', ' ') + '"'


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return str(float(value))


def put_number(fields, name, value):
    result = number(value)
    if result is not None:
        fields[name] = result


def emit(measurement, tags, fields, timestamp):
    return (measurement + ''.join(',' + k + '=' + tag(v) for k, v in tags.items()) +
            ' ' + ','.join(k + '=' + v for k, v in fields.items()) + ' ' + str(timestamp))


def get_stake(binary, socket, public_key, epoch):
    """Use Core's deprecated local commands while available; omit fields on errors."""
    address = command(binary, 'stake', 'pubkey2address', '--public_key', public_key).strip()
    if not re.fullmatch(r'oasis1[023456789acdefghjklmnpqrstuvwxyz]{38,}', address):
        raise ValueError('could not derive entity address')
    output = command(binary, 'stake', 'account', 'info', '-a', socket, '--stake.account.address', address)
    fields = {}
    section = re.search(r'Active Delegations to this Account:\s*\n\s*Total:\s*([0-9]+(?:\.[0-9]+)?)', output)
    if section:
        fields['activeStake'] = str(float(section.group(1)))
        # An empty or absent self entry in a successfully parsed section means zero.
        tail = output[section.end():]
        tail = re.split(r'\n(?=\S)', tail, maxsplit=1)[0]
        match = re.search(r'From:\s*' + re.escape(address) + r'\s*\(self\)\s*\n\s*Amount:\s*([0-9]+(?:\.[0-9]+)?)', tail)
        fields['selfStake'] = str(float(match.group(1))) if match else '0.0'
    # Rate schedule entries apply from their start epoch; do not use a future rate.
    commission = re.search(r'Commission Schedule:\s*\n(.*?)(?:\n\S|\Z)', output, re.S)
    if commission and isinstance(epoch, int):
        rates = re.findall(r'(?:start|Start):\s*(?:epoch\s+)?(\d+)\s*\n\s*(?:rate|Rate):\s*([0-9.]+)%', commission.group(1))
        applicable = [(int(start), rate) for start, rate in rates if int(start) <= epoch]
        if applicable:
            fields['commissionRate'] = str(float(max(applicable)[1]))
    return fields


def collect(binary, socket, runtime_id, stake):
    timestamp = time.time_ns()
    try:
        status = json.loads(command(binary, 'control', 'status', '-a', socket))
        consensus = status.get('consensus')
        registration = status.get('registration') or {}
        identity = status.get('identity') or {}
        descriptor = registration.get('descriptor') or {}
        if not isinstance(consensus, dict) or not isinstance(consensus.get('latest_height'), int):
            raise ValueError('missing consensus height in node status')
        entity = descriptor.get('entity_id') or os.getenv('OASIS_ENTITY_ID', '')
        node = identity.get('node') or os.getenv('OASIS_NODE_ID', '')
        if not entity or not node:
            raise ValueError('missing entity or node identity; configure OASIS_ENTITY_ID and OASIS_NODE_ID')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        print('Oasis status unavailable: ' + str(exc), file=sys.stderr)
        return emit('oasis_monitor_health', {'node': os.getenv('OASIS_MONITOR_NAME', 'oasis')}, {'up': '0.0'}, timestamp) + '\n', False

    fields = {}
    if isinstance(status.get('software_version'), str):
        fields['softwareVersion'] = field_string(status['software_version'])
    for key, value in [('latestHeight', consensus.get('latest_height')), ('latestEpoch', consensus.get('latest_epoch')),
                       ('registrationExpiration', descriptor.get('expiration'))]:
        put_number(fields, key, value)
    if isinstance(consensus.get('status'), str):
        fields['consensusStatus'] = field_string(consensus['status'])
    if isinstance(registration.get('last_attempt_successful'), bool):
        fields['registrationSuccessful'] = 'true' if registration['last_attempt_successful'] else 'false'
    is_validator = consensus.get('is_validator')
    if isinstance(is_validator, bool):
        fields['isValidator'] = 'true' if is_validator else 'false'
        fields['isValidatorInt'] = '1.0' if is_validator else '0.0'
    peers = (consensus.get('p2p') or {}).get('peers', consensus.get('node_peers'))
    if isinstance(peers, list):
        put_number(fields, 'peersCount', len(peers))
    if isinstance(registration.get('last_registration'), str):
        fields['registrationLastRegistration'] = field_string(registration['last_registration'][:19])
    runtimes = status.get('runtimes') or {}
    if not isinstance(runtimes, dict):
        runtimes = {}
    if runtime_id and runtime_id not in runtimes:
        print('Requested runtime is not present in node status: ' + runtime_id, file=sys.stderr)
    chosen = runtime_id if runtime_id in runtimes else (next(iter(runtimes)) if not runtime_id and len(runtimes) == 1 else None)
    if chosen:
        item = runtimes[chosen] or {}
        committee = item.get('committee') or {}
        for name, value in [('runtimesLatestRound', committee.get('latest_round', item.get('latest_round'))),
                            ('runtimesCommitteeLatestHeight', committee.get('latest_height'))]:
            put_number(fields, name, value)
        for name, key in [('committeePeersCount', 'peers'), ('runtimesExecutorRolesCount', 'executor_roles'),
                          ('runtimesStorageRolesCount', 'storage_roles')]:
            if key in committee and (committee[key] is None or isinstance(committee[key], list)):
                put_number(fields, name, len(committee[key] or []))
        storage = item.get('storage') or {}
        put_number(fields, 'runtimesStorageLastFinalizedRound', storage.get('last_finalized_round'))
    elif len(runtimes) > 1 and not runtime_id:
        print('Multiple runtimes: set OASIS_RUNTIME_ID for legacy dashboard runtime fields', file=sys.stderr)
    if stake:
        try:
            fields.update(get_stake(binary, socket, entity, consensus.get('latest_epoch')))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print('Stake metrics unavailable: ' + str(exc), file=sys.stderr)
    if os.getenv('OASIS_PRICE', '').lower() == 'coingecko':
        try:
            request = urllib.request.Request('https://api.coingecko.com/api/v3/simple/price?ids=oasis-network&vs_currencies=usd', headers={'User-Agent': 'oasismonitor/1'})
            with urllib.request.urlopen(request, timeout=4) as response:
                price = json.load(response)['oasis-network']['usd']
            put_number(fields, 'rosePrice', price)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print('Price unavailable: ' + str(exc), file=sys.stderr)
    tags = {'entity': entity, 'identityNode': node}
    lines = [emit('oasismonitor', tags, fields, timestamp),
             emit('oasis_monitor_health', {'node': os.getenv('OASIS_MONITOR_NAME', 'oasis')}, {'up': '1.0'}, timestamp)]
    for rid, item in runtimes.items():
        committee = (item or {}).get('committee') or {}
        values = {}
        if isinstance(committee.get('status'), str):
            values['status'] = field_string(committee['status'])
        put_number(values, 'latestRound', committee.get('latest_round', (item or {}).get('latest_round')))
        put_number(values, 'latestHeight', committee.get('latest_height'))
        if 'executor_roles' in committee and (committee['executor_roles'] is None or isinstance(committee['executor_roles'], list)):
            put_number(values, 'executorRolesCount', len(committee['executor_roles'] or []))
        if values:
            lines.append(emit('oasis_runtime', {**tags, 'runtime_id': rid}, values, timestamp))
    return '\n'.join(lines) + '\n', True


def write_atomic(path, data):
    parent = Path(path).parent
    # The directory must be controlled by the node account, never shared /tmp.
    fd, temp = tempfile.mkstemp(prefix='.metrics-', dir=parent, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(data)
            out.flush()
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='atomically replace a metrics file in a private service directory')
    args = parser.parse_args()
    binary = os.getenv('OASIS_NODE', '/usr/local/bin/oasis-node')
    socket = os.getenv('OASIS_SOCKET', 'unix:/node/data/internal.sock')
    runtime_id = os.getenv('OASIS_RUNTIME_ID', '')
    stake = os.getenv('OASIS_STAKE', 'true').lower() == 'true'
    data, ok = collect(binary, socket, runtime_id, stake)
    if args.output:
        write_atomic(args.output, data)
    else:
        sys.stdout.write(data)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
