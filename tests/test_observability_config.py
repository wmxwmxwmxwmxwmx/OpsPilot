import json
from pathlib import Path

import yaml


def test_dashboard_and_provisioning_match_real_metrics():
    root = Path('deploy/observability')
    dashboard = json.loads((root / 'grafana/dashboards/opspilot.json').read_text())
    assert dashboard['uid'] == 'opspilot-agent' and len(dashboard['panels']) >= 12
    assert len({p['id'] for p in dashboard['panels']}) == len(dashboard['panels'])
    for panel in dashboard['panels']:
        expr = panel['targets'][0]['expr']
        assert 'opspilot_' in expr
        assert all(label not in expr for label in ['workflow_id', 'session_id', 'trace_id', 'request_id'])
    scrape = yaml.safe_load((root / 'prometheus.yml').read_text())
    assert scrape['scrape_configs'][0]['job_name'] == 'opspilot'
    compose = yaml.safe_load((root / 'compose.yaml').read_text())
    assert all(':latest' not in s['image'] for s in compose['services'].values())
    assert all(port.startswith('127.0.0.1:') for s in compose['services'].values() for port in s['ports'])
    assert yaml.safe_load((root / 'tempo/tempo.yaml').read_text())['compactor']['compaction']['block_retention'] == '24h'
