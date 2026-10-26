"""Isolated released Guard Controller/Runner, PostgreSQL, CLI and LiteLLM."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

INTEGRATION = Path(__file__).resolve().parents[1]
GUARD = json.loads((INTEGRATION / 'guard-version.json').read_text())
BLOCKED = 'RELEASE_FORBIDDEN_MARKER'
SAFE = 'A safe release verification answer.'


class Stack:
    def __init__(self, directory):
        self.directory = directory
        self.prefix = 'guard-e2e-' + uuid4().hex[:10]
        self.containers = []
        self.secrets = []
        self.master = self.secret()
        self.password = self.secret()
        self.token = self.secret()
        self.image = os.environ['GUARD_TEST_RELAY_IMAGE']

    def secret(self):
        value = 'test-' + uuid4().hex
        self.secrets.append(value)
        return value

    def redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, '[redacted]')
        return value

    async def docker(self, *args, timeout=60):
        process = await asyncio.create_subprocess_exec('docker', *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        assert process.returncode == 0, self.redact(f'Docker {args[0]} failed: {stderr.decode()[-3000:]}')
        return (stdout + (stderr if args[0] == 'logs' else b'')).decode().strip()

    async def start(self, name, image, *, env=None, args=(), mounts=(), port=None, entrypoint=None):
        container = self.prefix + '-' + name
        command = ['run', '-d', '--name', container, '--network', self.prefix]
        for key, value in (env or {}).items():
            command += ['-e', f'{key}={value}']
        for source, target in mounts:
            command += ['-v', f'{source}:{target}:ro']
        if port:
            command += ['-p', f'127.0.0.1::{port}']
        if entrypoint:
            command += ['--entrypoint', entrypoint]
        # Register before starting so cleanup also covers partial Docker failures.
        self.containers.append(container)
        await self.docker(*command, image, *args, timeout=180)
        if port:
            address = await self.docker('port', container, f'{port}/tcp')
            assert address.startswith('127.0.0.1:') and '\n' not in address
            return container, 'http://' + address
        return container, None

    async def ready(self, url, path, container):
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            async with asyncio.timeout(180):
                while True:
                    try:
                        if (await client.get(url + path)).status_code == 200:
                            return
                    except httpx.TransportError:
                        pass
                    assert await self.docker('inspect', container, '--format', '{{.State.Running}}') == 'true'
                    await asyncio.sleep(.5)

    async def cli(self):
        result = await self.docker('exec',
            '-e', 'GUARD_TENANT_EMAIL=tenant@example.test', '-e', f'GUARD_TENANT_PASSWORD={self.password}',
            self.runner, 'guardctl', 'tenant', 'provision', '--url', self.origin,
            '--name', 'Provider release e2e', '--patch', '/tmp/policy.json',
            '--state', '/tmp/provision.json', '--adapter', 'litellm-generic-guardrail',
            '--emergency-reason', 'Isolated Provider integration validation',
            '--wait-seconds', '180', timeout=420)
        return json.loads(result)

    async def prepare(self):
        await self.docker('network', 'create', self.prefix)
        self.database, _ = await self.start('db', 'postgres:17-alpine', env={
            'POSTGRES_PASSWORD': 'synthetic-db-password', 'POSTGRES_DB': 'litellm'})
        async with asyncio.timeout(45):
            while True:
                try:
                    await self.docker('exec', self.database, 'pg_isready', '-U', 'postgres')
                    break
                except AssertionError:
                    await asyncio.sleep(.5)
        await self.docker('exec', self.database, 'createdb', '-U', 'postgres', 'guard')
        database = f'postgresql://postgres:synthetic-db-password@{self.database}:5432/'
        key = Ed25519PrivateKey.generate()
        private, public = self.directory / 'private.pem', self.directory / 'public.pem'
        private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        public.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        # Parent temporary directory is private; the mounted files must be readable
        # by the released images' non-root UID 65532.
        private.chmod(0o644)
        public.chmod(0o644)
        self.origin = f'http://{self.prefix}-controller:8080'
        self.runtime_origin = f'http://{self.prefix}-runner:8091'
        controller_env = {
            'CONTROLLER_DATABASE_URL': database + 'guard',
            'CONTROLLER_PUBLIC_URL': self.origin,
            'BETTER_AUTH_TRUSTED_ORIGINS': self.origin,
            'CONTROLLER_RUNTIME_SERVICE_URL': self.runtime_origin,
            'CONTROLLER_RUNTIME_PUBLIC_URL': self.runtime_origin,
            'CONTROLLER_ARTIFACT_SIGNING_KEY_PATH': '/tmp/private.pem',
            'CONTROLLER_RUNNER_TOKEN': self.token, 'CONTROLLER_METRICS_TOKEN': self.secret(),
            'CONTROLLER_GRPC_TRANSPORT': 'plaintext', 'BETTER_AUTH_SECRET': self.secret(),
            'CONTROLLER_BOOTSTRAP_USER_EMAIL': 'tenant@example.test',
            'CONTROLLER_BOOTSTRAP_USER_PASSWORD': self.password,
        }
        self.controller, self.controller_url = await self.start('controller', GUARD['controller_image'],
            env=controller_env, mounts=[(private, '/tmp/private.pem')], port=8080)
        await self.ready(self.controller_url, '/health/ready', self.controller)
        self.runner, self.runner_url = await self.start('runner', GUARD['runner_image'], env={
            'GUARD_CONTROLLER_TARGET': f'{self.controller}:9090',
            'GUARD_CONTROLLER_TOKEN': self.token,
            'GUARD_CONTROLLER_TELEMETRY_ENDPOINT': self.origin + '/api/internal/v1/runtime-events',
            'GUARD_ARTIFACT_PUBLIC_KEY_PATH': '/tmp/public.pem',
            'GUARD_RUNNER_ID': self.prefix, 'GUARD_RUNNER_COMPILER_CAPABLE': 'true',
        }, mounts=[(public, '/tmp/public.pem')], port=8091)
        await self.ready(self.runner_url, '/health/ready', self.runner)
        policy = self.directory / 'policy.json'
        policy.write_text(json.dumps({'runtimeProfile': 'auto', 'draftConfig': {
            'allowedTopics': [], 'restrictedTopics': [], 'topicControlMode': 'permissive',
            'outputDelivery': 'full_buffered', 'policyBindings': [{
                'policyId': 'keyword-blocking', 'policyVersion': '1.95.0', 'action': 'block',
                'parameterValues': {'blocked_words': BLOCKED},
                'enabledRuleIds': ['keyword/blocked-words'], 'ruleOrder': ['keyword/blocked-words'],
                'testCaseOverrides': {}, 'ruleActions': {}, 'enabledRails': ['input', 'output'],
                'reasoningPolicy': None,
            }],
        }}))
        await self.docker('cp', str(policy), self.runner + ':/tmp/policy.json')
        self.provision = await self.cli()
        assert self.provision['rollout'] == 'active'
        # Journals and credentials remain in the disposable Runner filesystem.
        state = json.loads(await self.docker('exec', self.runner, 'cat', '/tmp/provision.json'))
        self.credential = state['endpoint']['credential']
        self.credential_id = state['endpoint']['credentialId']
        self.secrets.append(self.credential)
        self.endpoint_path = '/runtime/v1/endpoints/' + self.provision['endpointId']
        self.endpoint = self.runtime_origin + self.endpoint_path
        assert (await self.docker('exec', self.runner, 'stat', '-c', '%a', '/tmp/provision.json')) == '600'
        self.model, self.model_url = await self.start('model', self.image,
            entrypoint='python', args=['/tmp/model_server.py'], port=5001,
            mounts=[(INTEGRATION / 'fixtures/model_server.py', '/tmp/model_server.py')])
        await self.ready(self.model_url, '/stats', self.model)
        config = self.directory / 'litellm.yaml'
        config.write_text(json.dumps({
            'model_list': [{'model_name': name, 'litellm_params': {
                'model': 'openai/' + model, 'api_key': 'synthetic-model-key',
                'api_base': f'http://{self.model}:5001/v1', 'num_retries': 0,
            }} for name, model in [('release-safe', 'safe'), ('release-blocked', 'unsafe-output')]],
            'litellm_settings': {'telemetry': False, 'num_retries': 0},
            'general_settings': {'store_model_in_db': True, 'master_key': 'os.environ/LITELLM_MASTER_KEY'},
        }))
        config.chmod(0o644)
        self.proxy, self.proxy_url = await self.start('proxy', self.image, port=4000,
            mounts=[(config, '/tmp/config.yaml')], env={
                'LITELLM_MASTER_KEY': self.master, 'LITELLM_SALT_KEY': self.secret(),
                'DATABASE_URL': database + 'litellm', 'STORE_MODEL_IN_DB': 'True',
                'LITELLM_MIGRATION_DIR': '/tmp/migrations',
            }, args=['--config', '/tmp/config.yaml', '--port', '4000', '--use_v2_migration_resolver'])
        await self.ready(self.proxy_url, '/health/readiness', self.proxy)

    async def cleanup(self, failed=False):
        errors = []
        for container in reversed(self.containers):
            if failed:
                try:
                    print(container + ':\n' + self.redact(await self.docker('logs', '--tail', '50', container)))
                except AssertionError:
                    pass
            try:
                await self.docker('rm', '-f', '-v', container)
            except AssertionError as error:
                errors.append(str(error))
        try:
            await self.docker('network', 'rm', self.prefix)
        except AssertionError as error:
            errors.append(str(error))
        (self.directory / 'private.pem').unlink(missing_ok=True)
        assert not errors, '\n'.join(errors)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if report.when == 'call':
        item.guard_test_failed = report.failed


@pytest_asyncio.fixture
async def guard_stack(tmp_path, request):
    stack = Stack(tmp_path)
    try:
        await stack.prepare()
        yield stack
    except BaseException:
        await stack.cleanup(failed=True)
        raise
    else:
        await stack.cleanup(failed=getattr(request.node, 'guard_test_failed', False))
