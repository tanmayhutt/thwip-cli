"""Real child-process transport checks, without invoking a provider."""

import asyncio
import sys

import pytest

from thwip.agents.native_rpc import NativeRPC


@pytest.mark.asyncio
async def test_real_process_rpc_roundtrip_and_cleanup(tmp_path):
    script = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " print(json.dumps({'id':m['id'],'result':{'method':m['method']}}),flush=True)\n"
    )
    rpc = await NativeRPC([sys.executable, '-u', '-c', script], str(tmp_path)).start()
    try:
        assert await rpc.request('probe', {}) == {'method': 'probe'}
    finally:
        await rpc.close()
    assert rpc.process.returncode is not None
    assert not rpc.pending


@pytest.mark.asyncio
async def test_request_timeout_does_not_leave_pending_future(tmp_path):
    rpc = await NativeRPC([sys.executable, '-c', 'import time; time.sleep(20)'], str(tmp_path)).start()
    try:
        with pytest.raises(TimeoutError):
            await rpc.request('probe', {}, timeout=0.05)
        assert not rpc.pending
    finally:
        await rpc.close()
    assert rpc.process.returncode is not None


@pytest.mark.asyncio
async def test_request_cancellation_cleans_pending_future(tmp_path):
    rpc = await NativeRPC([sys.executable, '-c', 'import time; time.sleep(20)'], str(tmp_path)).start()
    try:
        task = asyncio.create_task(rpc.request('probe', {}))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not rpc.pending
    finally:
        await rpc.close()
