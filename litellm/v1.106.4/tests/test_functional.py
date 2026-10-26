"""Provider lifecycle and non-streaming contracts against the pinned runtime.

Database operations are isolated mocks; encryption, HTTP and LiteLLM parsing
are real. Runner end-to-end tests live under integration/.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import litellm
from aiohttp import web
from fastapi import FastAPI
from litellm.exceptions import GuardrailRaisedException
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard import endpoints, service
from litellm.proxy.guardrails.guardrail_hooks.tasklattice_guard.tasklattice_guard import TaskLatticeGuard

ENDPOINT = 'http://127.0.0.1/runtime/v1/endpoints/00000000-0000-0000-0000-000000000001'
SECRET = 'synthetic-release-test-secret'


class FunctionalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.batch = Mock()
        self.batch.commit = AsyncMock()
        self.db = Mock()
        self.db.batch_.return_value = self.batch
        self.db.litellm_guardrailstable.find_unique = AsyncMock()
        self.client = SimpleNamespace(db=self.db)
        self.record = service.build_guardrail_record(ENDPOINT, 'test-credential', guardrail_id='test-id')
        self.db.litellm_guardrailstable.find_unique.return_value = self.record
        self.stack = []
        for target, value in [
            ('service.verify_connection', AsyncMock(return_value={'ready': True})),
            ('service.IN_MEMORY_GUARDRAIL_HANDLER', Mock()),
        ]:
            owner, attr = target.split('.')
            patcher = patch.object(globals()[owner], attr, value)
            self.stack.append(patcher)
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.stack)])
        self.enterContext(patch.object(litellm, 'credential_list', []))
        # Real encryption implementation, using a synthetic test key.
        self.enterContext(patch('litellm.proxy.common_utils.encrypt_decrypt_utils.get_salt_key', return_value='test-encryption-key'))

    async def create(self, **overrides):
        return await service.create_tasklattice_guard_connection(**{
            'prisma_client': self.client, 'endpoint': ENDPOINT, 'secret': SECRET,
            'guardrail_name': 'Release test', 'user_id': 'test-admin', **overrides})

    async def update(self, **overrides):
        return await service.update_tasklattice_guard_connection(**{
            'prisma_client': self.client, 'guardrail_id': 'test-id',
            'guardrail_name': None, 'endpoint': None, 'secret': None,
            'user_id': 'test-admin', **overrides})

    async def test_create_encrypts_secret_and_returns_masked_view(self):
        result = await self.create()
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(result['credential_configured'])
        saved = self.batch.litellm_credentialstable.create.call_args.kwargs['data']
        self.assertNotIn(SECRET, json.dumps(saved))
        from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
        values = saved['credential_values']
        if isinstance(values, str):
            values = json.loads(values)
        self.assertEqual(decrypt_value_helper(values['api_key'], key='api_key'), SECRET)
        guardrail = self.batch.litellm_guardrailstable.create.call_args.kwargs['data']
        self.assertNotIn(SECRET, json.dumps(guardrail, default=str))
        self.batch.commit.assert_awaited_once()

    async def test_invalid_connection_never_persists(self):
        service.verify_connection.side_effect = service.TaskLatticeGuardConnectionError('Not ready')
        with self.assertRaises(service.TaskLatticeGuardConnectionError):
            await self.create()
        self.db.batch_.assert_not_called()

    async def test_registration_failure_rolls_back_both_records_and_memory(self):
        service.IN_MEMORY_GUARDRAIL_HANDLER.initialize_guardrail.side_effect = RuntimeError('test failure')
        with self.assertRaises(RuntimeError):
            await self.create()
        self.assertEqual(self.batch.commit.await_count, 2)
        self.batch.litellm_guardrailstable.delete.assert_called_once()
        self.batch.litellm_credentialstable.delete.assert_called_once()
        self.assertEqual(litellm.credential_list, [])

    async def test_failed_commit_does_not_activate_credential_or_guardrail(self):
        self.batch.commit.side_effect = RuntimeError('test database failure')
        with self.assertRaises(RuntimeError):
            await self.create()
        self.assertEqual(litellm.credential_list, [])
        service.IN_MEMORY_GUARDRAIL_HANDLER.initialize_guardrail.assert_not_called()

    async def test_policy_update_preserves_credential_without_verification(self):
        result = await self.update(mode=['post_call'], timeout_seconds=3, default_on=False)
        self.assertEqual(result['mode'], ['post_call'])
        self.assertEqual(result['timeout_seconds'], 3)
        self.assertFalse(result['default_on'])
        service.verify_connection.assert_not_awaited()
        self.batch.litellm_credentialstable.update.assert_not_called()
        service.IN_MEMORY_GUARDRAIL_HANDLER.sync_guardrail_from_db.assert_called_once()

    async def test_endpoint_change_requires_secret(self):
        with self.assertRaises(service.TaskLatticeGuardConnectionError):
            await self.update(endpoint=ENDPOINT.replace('000001', '000002'))
        self.db.batch_.assert_not_called()

    async def test_secret_rotation_verifies_and_encrypts_new_value(self):
        result = await self.update(secret='rotated-test-secret')
        service.verify_connection.assert_awaited_once_with(ENDPOINT, 'rotated-test-secret')
        self.assertNotIn('rotated-test-secret', json.dumps(result))
        saved = self.batch.litellm_credentialstable.update.call_args.kwargs['data']
        self.assertNotIn('rotated-test-secret', json.dumps(saved))

    async def test_failed_rotation_preserves_records(self):
        service.verify_connection.side_effect = service.TaskLatticeGuardConnectionError('Not ready')
        with self.assertRaises(service.TaskLatticeGuardConnectionError):
            await self.update(secret='wrong-test-secret')
        self.db.batch_.assert_not_called()

    async def test_delete_removes_guardrail_and_credential(self):
        await service.delete_tasklattice_guard_connection(prisma_client=self.client, guardrail_id='test-id')
        self.batch.litellm_guardrailstable.delete.assert_called_once()
        self.batch.litellm_credentialstable.delete.assert_called_once_with(where={'credential_name': 'test-credential'})
        service.IN_MEMORY_GUARDRAIL_HANDLER.delete_in_memory_guardrail.assert_called_once_with('test-id')

    async def test_missing_and_other_provider_records_are_rejected(self):
        for record in [None, {**self.record, 'litellm_params': {'guardrail': 'other'}}]:
            with self.subTest(record=record):
                self.db.litellm_guardrailstable.find_unique.return_value = record
                with self.assertRaises(service.TaskLatticeGuardConnectionError):
                    await self.update()
        self.db.batch_.assert_not_called()

    async def test_management_endpoints_require_admin(self):
        app = FastAPI()
        app.include_router(endpoints.router)
        app.dependency_overrides[endpoints.user_api_key_auth] = lambda: UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for method, url, body in [
                ('POST', '/guardrails/tasklattice', {'endpoint': ENDPOINT, 'secret': SECRET}),
                ('GET', '/guardrails/tasklattice/test-id', None),
                ('PATCH', '/guardrails/tasklattice/test-id', {}),
                ('DELETE', '/guardrails/tasklattice/test-id', None),
            ]:
                with self.subTest(method=method):
                    response = await client.request(method, url, json=body)
                    self.assertEqual(response.status_code, 403)

    def test_invalid_policy_and_endpoint_configuration_is_rejected(self):
        for params in [dict(mode=[]), dict(mode=['pre_call', 'pre_call']), dict(mode=['during_call']),
                       dict(timeout_seconds=True), dict(timeout_seconds=0), dict(timeout_seconds=61),
                       dict(timeout_seconds=1.5), dict(default_on='false'), dict(unreachable_fallback='other')]:
            with self.subTest(params=params), self.assertRaises(service.TaskLatticeGuardConnectionError):
                service.build_guardrail_record(ENDPOINT, 'credential', **params)
        for endpoint in ['file:///etc/passwd', ENDPOINT+'?secret=x', ENDPOINT+'#secret',
                         ENDPOINT.replace('127.0.0.1', 'user:secret@127.0.0.1'), ENDPOINT+'/verify']:
            with self.subTest(endpoint=endpoint), self.assertRaises(service.TaskLatticeGuardConnectionError):
                service.normalize_endpoint(endpoint)


class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.status = 200
        self.payload = {'action': 'NONE'}
        self.requests = []
        async def handler(request):
            self.requests.append((request.path, request.headers.get('x-api-key'), await request.json()))
            return web.json_response(self.payload, status=self.status)
        app = web.Application()
        app.router.add_post('/{tail:.*}', handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.endpoint = ENDPOINT.replace('127.0.0.1', '127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]))
        self.addAsyncCleanup(self.runner.cleanup)
        self.enterContext(patch.object(service.CredentialAccessor, 'get_credential_values', return_value={'api_key': SECRET}))
        self.provider = TaskLatticeGuard(api_base=self.endpoint, credential_name='test', timeout_seconds=1,
                                        guardrail_name='test', event_hook=['pre_call', 'post_call'], default_on=True)

    async def apply(self, stage='request'):
        return await self.provider.apply_guardrail(inputs={'texts': ['private input']}, request_data={}, input_type=stage)

    async def test_allow_and_transform_in_both_stages(self):
        for stage in ['request', 'response']:
            for action, text in [('NONE', 'private input'), ('GUARDRAIL_INTERVENED', '[REDACTED]')]:
                self.payload = {'action': action, 'texts': [text]}
                result = await self.apply(stage)
                self.assertEqual(result['texts'], [text])
                self.assertEqual(self.requests[-1][1], SECRET)
                self.assertEqual(self.requests[-1][2]['input_type'], stage)

    async def test_policy_block_cannot_fail_open(self):
        self.provider.unreachable_fallback = 'fail_open'
        self.payload = {'action': 'BLOCKED', 'blocked_reason': 'Policy block'}
        with self.assertRaises(GuardrailRaisedException):
            await self.apply()

    async def test_only_gateway_outages_can_fail_open(self):
        for status in [401, 403, 429, 500, 502, 503, 504]:
            for fallback in ['fail_closed', 'fail_open']:
                self.status = status
                self.provider.unreachable_fallback = fallback
                with self.subTest(status=status, fallback=fallback):
                    if status in [502, 503, 504] and fallback == 'fail_open':
                        self.assertEqual((await self.apply())['texts'], ['private input'])
                    else:
                        with self.assertRaises(Exception):
                            await self.apply()

    async def test_timeout_passed_on_every_callback_and_fail_on_error_forced(self):
        provider = TaskLatticeGuard(api_base=self.endpoint, credential_name='test', timeout_seconds=3, fail_on_error=False)
        self.assertTrue(provider.fail_on_error)
        with patch.object(provider.async_handler, 'post', AsyncMock(return_value=httpx.Response(200,
                request=httpx.Request('POST', self.endpoint), json={'action': 'NONE'}))) as post:
            await provider.apply_guardrail(inputs={'texts': ['test']}, request_data={}, input_type='request')
            self.assertEqual(post.call_args.kwargs['timeout'], 3.0)

    async def test_missing_runtime_credential_fails_closed(self):
        with patch.object(service.CredentialAccessor, 'get_credential_values', return_value={}):
            with self.assertRaises(Exception):
                await self.apply()
        self.assertEqual(self.requests, [])

    async def test_verification_requires_ready_matching_adapter_and_protocol(self):
        ready = {'ready': True, 'adapter_id': 'litellm-generic-guardrail', 'protocol': 'litellm'}
        self.payload = ready
        self.assertEqual(await service.verify_connection(self.endpoint, SECRET), ready)
        self.assertTrue(self.requests[-1][0].endswith('/verify'))
        for payload in [{}, {**ready, 'ready': False}, {**ready, 'ready': 1},
                        {**ready, 'adapter_id': 'other'}, {**ready, 'protocol': 'other'}]:
            self.payload = payload
            with self.subTest(payload=payload), self.assertRaises(service.TaskLatticeGuardConnectionError):
                await service.verify_connection(self.endpoint, SECRET)

    async def test_failed_verification_never_exposes_secret(self):
        for status in [302, 401, 500]:
            self.status, self.payload = status, {'detail': SECRET}
            with self.assertRaises(service.TaskLatticeGuardConnectionError) as caught:
                await service.verify_connection(self.endpoint, SECRET)
            self.assertNotIn(SECRET, str(caught.exception))
