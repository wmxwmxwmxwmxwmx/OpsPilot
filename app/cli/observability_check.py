"""Live acceptance: python -m app.cli.observability_check --help.

Runs a mock diagnosis (writes a new workflow/incident), never changes server config.
"""
import argparse
import json
import math
import os
import time
from pathlib import Path

import httpx


def check(args):
    token = os.environ['WORKFLOW_API_TOKEN']
    password = os.environ['GRAFANA_ADMIN_PASSWORD']
    with httpx.Client(trust_env=False, timeout=30) as client:
        def diagnose():
            response = client.post(args.app + '/api/v1/workflows',
                headers={'Authorization': 'Bearer ' + token}, json={'message':
                'Redis 缓存命中率突然下降，请帮我排查原因，并参考运维 Runbook 给出建议。'})
            response.raise_for_status()
            assert response.json()['status'] == 'completed', response.json().get('error_code')
            return response

        def sample_count():
            response = client.get(args.prometheus + '/api/v1/query', params={'query':
                'sum(opspilot_operation_duration_seconds_count{component="rag"})'})
            response.raise_for_status()
            samples = response.json()['data']['result']
            return float(samples[0]['value'][1]) if samples else None

        # A first observation creates the labelled histogram. Scrape it before
        # the measured request so rate() can observe a real increase.
        before = sample_count()
        warmup = before is None
        if warmup:
            diagnose()
            deadline = time.monotonic() + 60
            while before is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError('No RAG histogram scraped after warmup')
                time.sleep(1)
                before = sample_count()
        response = diagnose()
        diagnosis = response.json()
        assert diagnosis['status'] == 'completed', diagnosis.get('error_code')
        assert 'chunk:' in diagnosis['answer'] and 'redis/' in diagnosis['answer']
        trace_id = response.headers['x-trace-id']
        deadline = time.monotonic() + 60
        while True:
            targets = client.get(args.prometheus + '/api/v1/targets').json()['data']['activeTargets']
            up = any(t['labels'].get('job') == 'opspilot' and t['health'] == 'up' for t in targets)
            trace = client.get(args.tempo + '/api/traces/' + trace_id)
            after = sample_count()
            if up and trace.status_code == 200 and after is not None and after > before:
                break
            if time.monotonic() >= deadline: raise RuntimeError('Monitoring readiness deadline exceeded')
            time.sleep(1)
        auth = (args.grafana_user, password)
        for uid in ('opspilot-prometheus', 'opspilot-tempo'):
            result = client.get(args.grafana + '/api/datasources/uid/' + uid + '/health', auth=auth)
            result.raise_for_status()
            assert result.json()['status'] == 'OK', uid
        dashboard = client.get(args.grafana + '/api/dashboards/uid/opspilot-agent', auth=auth)
        dashboard.raise_for_status()
        # Query the actual provisioned histogram panel through Grafana's proxy.
        expression = next(p['targets'][0]['expr'] for p in dashboard.json()['dashboard']['panels']
                          if p['title'] == 'RAG Retrieval Duration P95')
        query = client.get(args.grafana + '/api/datasources/proxy/uid/opspilot-prometheus/api/v1/query',
                           params={'query': expression}, auth=auth)
        query.raise_for_status()
        assert query.json()['status'] == 'success'
        samples = query.json()['data']['result']
        assert samples and all(math.isfinite(float(row['value'][1])) for row in samples), (
            'Histogram needs two scrape samples and observations within the 5m window; rerun after the next scrape')
        proxied = client.get(args.grafana + '/api/datasources/proxy/uid/opspilot-tempo/api/traces/' + trace_id, auth=auth)
        proxied.raise_for_status()
        payload = proxied.json()
        batches = payload.get('batches', payload.get('resourceSpans', []))
        spans = [span for batch in batches for scope in batch.get('scopeSpans', []) for span in scope.get('spans', [])]
        names = {span['name'] for span in spans}
        assert {'workflow.execution', 'rag.retrieve', 'vector.search', 'embedding.generate', 'mcp.call',
                'remote_tool.get_mock_redis_metrics', 'llm.generate', 'persistence.incident'} <= names, names
        report = {'workflow_id': diagnosis['workflow_id'], 'incident_id': diagnosis['incident_id'],
            'trace_id': trace_id, 'prometheus_target': 'up', 'grafana_dashboard': 'opspilot-agent',
            'span_count': len(spans), 'span_names': sorted(names), 'warmup_workflow_created': warmup,
            'rag_samples_before': before, 'rag_samples_after': after,
            'rag_p95_seconds': float(samples[0]['value'][1]),
            'embedding_provider': 'see runtime configuration'}
        if args.output: Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [('app', 'http://127.0.0.1:8000'), ('prometheus', 'http://127.0.0.1:9090'),
                          ('grafana', 'http://127.0.0.1:3000'), ('tempo', 'http://127.0.0.1:3200')]:
        parser.add_argument('--' + name, default=default)
    parser.add_argument('--grafana-user', default='admin')
    parser.add_argument('--output')
    check(parser.parse_args())


if __name__ == '__main__':
    main()
