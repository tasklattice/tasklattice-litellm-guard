"""CLI-provisioned Guard v0.3.12 -> LiteLLM management, persistence and inference."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

BLOCKED = 'RELEASE_FORBIDDEN_MARKER'
SAFE = 'A safe release verification answer.'


async def assert_protection(client, stack):
    async def complete(model='release-safe', text='A harmless question.', stream=False):
        return await client.post('/v1/chat/completions', json={
            'model': model, 'messages': [{'role': 'user', 'content': text}], 'stream': stream})

    safe = await complete()
    assert safe.status_code == 200
    assert safe.json()['choices'][0]['message']['content'] == SAFE
    async with httpx.AsyncClient(base_url=stack.model_url, trust_env=False) as model:
        before = (await model.get('/stats')).json()['calls']
        blocked = await complete(text=BLOCKED)
        assert 400 <= blocked.status_code < 500
        assert (await model.get('/stats')).json()['calls'] == before, 'Input block must precede the model call'
    blocked = await complete(model='release-blocked')
    assert 400 <= blocked.status_code < 500
    for name in ['release-safe', 'release-blocked']:
        response = await complete(model=name, stream=True)
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        text, finishes, errors = '', [], []
        for event in events:
            if event.get('error'):
                errors.append(event['error'])
            for choice in event.get('choices', []):
                text += choice.get('delta', {}).get('content') or ''
                if choice.get('finish_reason'):
                    finishes.append(choice['finish_reason'])
        if name == 'release-safe':
            assert response.status_code == 200 and text == SAFE
            assert finishes == ['stop'] and not errors
        else:
            assert text == '' and not finishes
            assert errors or 400 <= response.status_code < 500
            assert BLOCKED not in response.text


@pytest.mark.asyncio
async def test_management_api_and_encrypted_persistence(guard_stack):
    stack = guard_stack
    # A repeat of the exact CLI operation must reuse the published resources.
    resumed = await stack.cli()
    assert resumed == stack.provision
    async with httpx.AsyncClient(base_url=stack.proxy_url,
            headers={'Authorization': 'Bearer ' + stack.master}, timeout=15, trust_env=False) as client:
        assert (await client.get('/ui/')).status_code == 200
        endpoint = stack.endpoint
        secret = stack.credential
        payload = {'guardrail_name': 'Release lifecycle', 'endpoint': endpoint,
                   'secret': secret, 'default_on': True}
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
            return await stack.docker('exec', stack.database, 'psql', '-U', 'postgres', '-d', 'litellm', '-Atc',
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
        await assert_protection(client, stack)
        updated = await client.patch(path, json={'mode': ['pre_call'], 'timeout_seconds': 3})
        assert updated.status_code == 200 and updated.json()['timeout_seconds'] == 3
        assert await stored_credentials() == before
        changed_endpoint = endpoint.rsplit('/', 1)[0] + '/' + str(uuid4())
        assert (await client.patch(path, json={'endpoint': changed_endpoint})).status_code == 400
        assert (await client.patch(path, json={'secret': 'wrong'})).status_code == 400
        assert await stored_credentials() == before
        async with httpx.AsyncClient(base_url=stack.controller_url,
                headers={'Origin': stack.origin}, timeout=30, trust_env=False) as control:
            login = await control.post('/api/auth/sign-in/email', json={
                'email': 'tenant@example.test', 'password': stack.password})
            assert login.status_code == 200
            rotation = await control.post('/api/v1/endpoints/' + stack.provision['endpointId'] + '/credentials')
            assert rotation.status_code == 201
            rotated = rotation.json()['credential']
            stack.secrets.append(rotated)
            revoked = await control.delete('/api/v1/endpoints/' + stack.provision['endpointId']
                + '/credentials/' + stack.credential_id)
            assert revoked.status_code == 204
        async with httpx.AsyncClient(timeout=5, trust_env=False) as runtime:
            async with asyncio.timeout(30):
                while True:
                    verified = await runtime.post(stack.runner_url + stack.endpoint_path + '/verify',
                        headers={'x-api-key': rotated}, json={})
                    stale = await runtime.post(stack.runner_url + stack.endpoint_path + '/verify',
                        headers={'x-api-key': secret}, json={})
                    if verified.status_code == 200 and stale.status_code in [401, 403]:
                        break
                    await asyncio.sleep(.2)
        # Prove the old credential no longer authorizes model traffic.
        async with httpx.AsyncClient(base_url=stack.model_url, trust_env=False) as model:
            calls_before = (await model.get('/stats')).json()['calls']
            denied = await client.post('/v1/chat/completions', json={
                'model': 'release-safe', 'messages': [{'role': 'user', 'content': 'Safe question.'}]})
            assert denied.status_code >= 400
            assert (await model.get('/stats')).json()['calls'] == calls_before
        assert (await client.patch(path, json={'secret': rotated})).status_code == 200
        after = await stored_credentials()
        assert after != before and rotated not in after
        assert (await client.patch(path, json={'mode': ['pre_call', 'post_call'], 'timeout_seconds': 10})).status_code == 200
        await assert_protection(client, stack)
        # Ordinary LiteLLM deletion must invoke Provider credential cleanup.
        assert (await client.delete('/guardrails/' + guardrail_id)).status_code == 200
        assert (await client.get(path)).status_code == 404
        assert await stored_credentials() == ''
        # Also exercise the dedicated deletion route.
        payload['secret'] = rotated
        response = await client.post('/guardrails/tasklattice', json=payload)
        assert response.status_code == 200
        assert (await client.delete('/guardrails/tasklattice/' + response.json()['guardrail_id'])).status_code == 204
        assert await stored_credentials() == ''
