"""Run the pinned Guard repository's real Runner/NeMo tests on the built image.

Docker Desktop uses host.docker.internal. Linux CI shares the host network on
random ports so the same assertions work without exposing mock servers on LAN.
Only Docker transport setup changes; Guard's test scenarios are unchanged.
"""
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import pytest

root = Path(os.environ['GUARD_CHECKOUT']).resolve()
sys.path.insert(0, str(root))
from tests.e2e import test_relay_stream_delivery as suite


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interruptible', 'window_buffered', 'full_buffered'])
async def test_guard_stream_delivery(tmp_path, mode):
    assert os.environ.get('GUARD_TEST_RELAY_IMAGE'), 'A built image is required; never skip this release gate'
    assert not os.environ.get('GUARD_TEST_LITELLM_GUARD_CHECKOUT'), 'Test the baked Provider, not mounted code'
    assert not os.environ.get('GUARD_TEST_RELAY_PYTHON'), 'Test the built image'
    original = suite.docker
    ports = {}

    async def docker(*args):
        if sys.platform != 'linux':
            return await original(*args)
        args = list(args)
        if args[0] == 'run':
            name = args[args.index('--name') + 1]
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                ports[name] = listener.getsockname()[1]
            index = args.index('-p')
            args[index:index + 2] = ['--network', 'host']
            args[args.index('--port') + 1] = str(ports[name])
            args[args.index('--host') + 1] = '127.0.0.1'
            args = [arg.replace('host.docker.internal', '127.0.0.1') for arg in args]
        elif args[0] == 'port':
            return f'127.0.0.1:{ports[args[1]]}'
        return await original(*args)

    with patch.object(suite, 'docker', docker):
        await suite.test_actual_relay_stream_delivery_and_cancellation(tmp_path, mode)
