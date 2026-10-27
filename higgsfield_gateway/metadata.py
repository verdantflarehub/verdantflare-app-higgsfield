"""Read the installed channel declaration; missing metadata never invents models."""
import hashlib
import json
import os
from pathlib import Path

import yaml


def capabilities(env=None):
    env = os.environ if env is None else env
    path = Path(env.get('CHANNEL_METADATA_FILE', '/etc/verdantflare/channel.yaml'))
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError('metadata_too_large')
        data = yaml.safe_load(raw)
        rows = data['capabilities']
        if data.get('schema_version') != 1 or data.get('channel_id') != 'higgsfield' or not isinstance(rows, list):
            raise ValueError('invalid_metadata')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or row.get('type') != 'video'
                    or not all(isinstance(row.get(k), str) and row[k] for k in ('capability_id', 'model_id', 'display_name', 'execution_state'))
                    or row['capability_id'] in seen or not isinstance(row.get('parameter_schema'), dict)):
                raise ValueError('invalid_metadata')
            seen.add(row['capability_id'])
        canonical = json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        return {'schema_version': 1, 'channel_id': 'higgsfield', 'version': 'sha256:' + hashlib.sha256(canonical).hexdigest(),
                'capabilities': rows}
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        raise ValueError('capabilities_unavailable') from None
