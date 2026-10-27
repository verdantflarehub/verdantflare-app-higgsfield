import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from higgsfield_gateway.metadata import capabilities


class MetadataTest(unittest.TestCase):
    def test_missing_metadata_never_returns_static_models(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'capabilities_unavailable'):
                capabilities({'CHANNEL_METADATA_FILE': str(Path(folder) / 'missing.yaml')})

    def test_installed_metadata_controls_names_schema_and_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'channel.yaml'
            row = {'capability_id':'video.generate.test', 'model_id':'model-test', 'display_name':'Readable name',
                   'type':'video', 'execution_state':'adapter_connected_unverified',
                   'parameter_schema':{'type':'object','properties':{'duration_seconds':{'minimum':4}}}}
            doc = {'schema_version':1,'channel_id':'higgsfield','capabilities':[row]}
            path.write_text(json.dumps(doc))
            env = {'CHANNEL_METADATA_FILE':str(path)}
            first = capabilities(env)
            self.assertEqual(first['capabilities'], [row])
            row['display_name'] = 'Updated name'
            path.write_text(json.dumps(doc))
            self.assertNotEqual(first['version'], capabilities(env)['version'])
            doc['capabilities'].append(row)
            path.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):
                capabilities(env)
