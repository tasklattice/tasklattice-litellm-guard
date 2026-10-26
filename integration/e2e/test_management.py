"""Real management API and encrypted persistence on an isolated PostgreSQL.

The verification peer is controlled; actual Runner/NeMo is tested separately.
All credentials here are generated test values and all containers are removed.
"""
import asyncio
import os
from uuid import uuid4

import httpx
import pytest


async def docker(*args, timeout=60):
    process = await asyncio.create_subprocess_exec('docker', *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    assert process.returncode == 0, f'Docker {args[0]} failed (arguments and output omitted)'
    return stdout.decode().strip()


@pytest.mark.asyncio
async def test_management_api_and_encrypted_persistence(tmp_path):
    image = os.environ['GUARD_TEST_RELAY_IMAGE']
    prefix = 'guard-release-' + uuid4().hex[:10]
    database, peer, proxy = [prefix + '-' + name for name in ['db', 'peer', 'proxy']]
    master, secret = 'sk-test-' + uuid4().hex, 'test-' + uuid4().hex
    config = tmp_path / 'litellm.yaml'
    config.write_text('model_list: []\nlitellm_settings:\n  telemetry: false\ngeneral_settings:\n  store_model_in_db: true\n  master_key: os.environ/LITELLM_MASTER_KEY\n')
    config.chmod(0o644)
    # Only synthetic data is accepted; this is a connection-verification peer.
    peer_script = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('content-length', 0)))
        self.send_response(401 if self.headers.get('x-api-key') == 'wrong' else 200)
        self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'ready': True, 'adapter_id': 'litellm-generic-guardrail', 'protocol': 'litellm'}).encode())
HTTPServer(('0.0.0.0', 5001), Handler).serve_forever()
'''
    containers = []
    await docker('network', 'create', prefix)
    try:
        await docker('run', '-d', '--name', database, '--network', prefix,
            '-e', 'POSTGRES_PASSWORD=synthetic-db-password', '-e', 'POSTGRES_DB=litellm',
            'postgres:17-alpine', timeout=180)
        containers.append(database)
        async with asyncio.timeout(45):
            while True:
                try:
                    await docker('exec', database, 'pg_isready', '-U', 'postgres')
                    break
                except AssertionError:
                    await asyncio.sleep(.5)
        await docker('run', '-d', '--pull=never', '--name', peer, '--network', prefix,
            '--entrypoint', 'python', image, '-c', peer_script)
        containers.append(peer)
        # Runtime startup can race an otherwise healthy verification peer on CI.
        async with asyncio.timeout(30):
            while True:
                try:
                    await docker('exec', peer, 'python', '-c',
                        "import socket; socket.create_connection(('127.0.0.1', 5001), timeout=1).close()")
                    break
                except AssertionError:
                    assert await docker('inspect', peer, '--format', '{{.State.Running}}') == 'true'
                    await asyncio.sleep(.2)
        await docker('run', '-d', '--pull=never', '--name', proxy, '--network', prefix,
            '-p', '127.0.0.1::4000', '-v', f'{config}:/tmp/config.yaml:ro',
            '-e', f'LITELLM_MASTER_KEY={master}', '-e', 'LITELLM_SALT_KEY=synthetic-salt',
            '-e', f'DATABASE_URL=postgresql://postgres:synthetic-db-password@{database}:5432/litellm',
            '-e', 'STORE_MODEL_IN_DB=True', '-e', 'LITELLM_MIGRATION_DIR=/tmp/migrations',
            image, '--config', '/tmp/config.yaml', '--port', '4000', '--use_v2_migration_resolver')
        containers.append(proxy)
        address = await docker('port', proxy, '4000/tcp')
        assert address.startswith('127.0.0.1:') and '\n' not in address
        async with httpx.AsyncClient(base_url='http://' + address,
                headers={'Authorization': 'Bearer ' + master}, timeout=15, trust_env=False) as client:
            async with asyncio.timeout(180):
                while True:
                    try:
                        if (await client.get('/health/readiness')).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert await docker('inspect', proxy, '--format', '{{.State.Running}}') == 'true'
                    await asyncio.sleep(1)
            assert (await client.get('/ui/')).status_code == 200
            endpoint = f'http://{peer}:5001/runtime/v1/endpoints/{uuid4()}'
            payload = {'guardrail_name': 'Release lifecycle', 'endpoint': endpoint,
                       'secret': secret, 'default_on': False}
            # Failed verification must not persist anything.
            failed = await client.post('/guardrails/tasklattice', json={**payload, 'secret': 'wrong'})
            assert failed.status_code == 400
            response = await client.post('/guardrails/tasklattice', json=payload)
            assert response.status_code == 200, response.text.replace(secret, '[redacted]')
            value = response.json()
            guardrail_id = value['guardrail_id']
            assert value['credential_configured'] and secret not in response.text
            path = '/guardrails/tasklattice/' + guardrail_id
            read = await client.get(path)
            assert read.status_code == 200 and secret not in read.text

            async def stored_credentials():
                return await docker('exec', database, 'psql', '-U', 'postgres', '-d', 'litellm', '-Atc',
                    'SELECT credential_values::text FROM "LiteLLM_CredentialsTable"')
            before = await stored_credentials()
            assert before and secret not in before
            generic = await client.post('/guardrails', json={'guardrail': {'guardrail_name': 'Bypass',
                'litellm_params': {'guardrail': 'tasklattice_guard', 'mode': ['pre_call'],
                                  'api_base': endpoint, 'api_key': secret}}})
            assert generic.status_code == 400
            replacement = {'guardrail': {'guardrail_name': 'Bypass', 'litellm_params': {
                'guardrail': 'tasklattice_guard', 'mode': ['pre_call'], 'api_key': secret}}}
            for method, body in [('PUT', replacement), ('PATCH', {'litellm_params': {'api_key': secret}})]:
                generic = await client.request(method, '/guardrails/' + guardrail_id, json=body)
                assert generic.status_code == 400
            updated = await client.patch(path, json={'mode': ['pre_call'], 'timeout_seconds': 3})
            assert updated.status_code == 200 and updated.json()['timeout_seconds'] == 3
            assert await stored_credentials() == before
            changed_endpoint = endpoint.rsplit('/', 1)[0] + '/' + str(uuid4())
            assert (await client.patch(path, json={'endpoint': changed_endpoint})).status_code == 400
            assert (await client.patch(path, json={'secret': 'wrong'})).status_code == 400
            assert await stored_credentials() == before
            rotated = 'rotated-' + uuid4().hex
            assert (await client.patch(path, json={'secret': rotated})).status_code == 200
            after = await stored_credentials()
            assert after != before and rotated not in after
            # Ordinary LiteLLM deletion must invoke Provider credential cleanup.
            assert (await client.delete('/guardrails/' + guardrail_id)).status_code == 200
            assert (await client.get(path)).status_code == 404
            assert await stored_credentials() == ''
            # Also exercise the dedicated deletion route.
            response = await client.post('/guardrails/tasklattice', json=payload)
            assert response.status_code == 200
            assert (await client.delete('/guardrails/tasklattice/' + response.json()['guardrail_id'])).status_code == 204
            assert await stored_credentials() == ''
    except Exception:
        for container in containers:
            process = await asyncio.create_subprocess_exec('docker', 'logs', '--tail', '60', container,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(process.communicate(), 15)
            text = output.decode().replace(master, '[redacted]').replace(secret, '[redacted]')
            print(f'{container}:\n{text[-6000:]}')
        raise
    finally:
        for container in reversed(containers):
            await docker('rm', '-f', '-v', container)
        await docker('network', 'rm', prefix)
