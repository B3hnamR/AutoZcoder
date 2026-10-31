"""Updater admission/single-flight contract; no processes or network changes."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from zcode_gateway import api_dash


def state():
    return SimpleNamespace(pool=SimpleNamespace(snapshot=AsyncMock(return_value={'detail':[]})),
                           generator=None, cfg=SimpleNamespace(),
                           updater=SimpleNamespace(status={'update_available':True,'latest':'9.0.0'}))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['auto','paused','claim_batch','worker','claim','checker','request'])
async def test_apply_blocks_all_active_work(kind):
    s=state()
    s.generator=SimpleNamespace(auto_generate=SimpleNamespace(status=lambda:{'status':'idle'}),
        _claim_all_running=False,workers={},claims={},checker=SimpleNamespace(alive=lambda:False))
    if kind in ('auto','paused'):
        s.generator.auto_generate.status=lambda:{'status':'paused' if kind=='paused' else 'running'}
    elif kind=='claim_batch':s.generator._claim_all_running=True
    elif kind=='worker':s.generator.workers={'w':SimpleNamespace(done=SimpleNamespace(is_set=lambda:False))}
    elif kind=='claim':s.generator.claims={'c':SimpleNamespace(alive=lambda:True)}
    elif kind=='checker':s.generator.checker.alive=lambda:True
    else:s.pool.snapshot=AsyncMock(return_value={'detail':[{'in_flight':1}]})
    with pytest.raises(HTTPException) as error:
        await api_dash._check_update_idle(s)
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_download_singleflight_and_ready_state(tmp_path,monkeypatch):
    s=state()
    s.updater=SimpleNamespace(status={'update_available':True,'latest':'9.0.0','download_url':'https://github.com/a/exe','checksum_url':'https://github.com/a/checksums'})
    monkeypatch.setattr(api_dash,'get_state',lambda _:s)
    monkeypatch.setattr(api_dash,'_windows_update_only',lambda:None)
    from zcode_gateway import updater
    entered,release=asyncio.Event(),asyncio.Event()
    target=tmp_path/'ZcodeGateway.new.exe'
    target.write_bytes(b'dummy')
    async def download(*args):
        entered.set();await release.wait();return target
    monkeypatch.setattr(updater,'download_update',download)
    monkeypatch.setattr(updater,'exe_dir',lambda:tmp_path)
    first=asyncio.create_task(api_dash.update_download(None,'admin'))
    await entered.wait()
    with pytest.raises(HTTPException) as error:
        await api_dash.update_download(None,'admin')
    assert error.value.status_code==409
    assert (await api_dash.update_status(None,'admin'))['updater_phase']=='downloading'
    release.set();assert (await first)['size']==5
    assert s.update_phase=='ready'
    assert (await api_dash.update_download(None,'admin'))['size']==5


@pytest.mark.asyncio
async def test_failed_download_releases_busy_phase(monkeypatch):
    s=state();s.updater=SimpleNamespace(status={'update_available':True,'latest':'9.0.0','download_url':'x'})
    monkeypatch.setattr(api_dash,'get_state',lambda _:s)
    monkeypatch.setattr(api_dash,'_windows_update_only',lambda:None)
    monkeypatch.setattr('zcode_gateway.updater.download_update',AsyncMock(side_effect=RuntimeError('fixture')))
    with pytest.raises(HTTPException):await api_dash.update_download(None,'admin')
    assert s.update_phase=='idle'


@pytest.mark.asyncio
async def test_busy_apply_restores_ready_phase_without_spawning(monkeypatch):
    s=state();s.update_phase='ready'
    s.pool.snapshot=AsyncMock(return_value={'detail':[{'in_flight':1}]})
    monkeypatch.setattr(api_dash,'get_state',lambda _:s)
    monkeypatch.setattr(api_dash,'_windows_update_only',lambda:None)
    with pytest.raises(HTTPException) as error:await api_dash.update_apply(None,'admin')
    assert error.value.status_code==409 and s.update_phase=='ready'


@pytest.mark.asyncio
async def test_applying_blocks_new_generator_and_client_admission(monkeypatch):
    s=state();s.update_phase='applying'
    monkeypatch.setattr(api_dash,'get_state',lambda _:s)
    with pytest.raises(HTTPException) as error:api_dash._generator(None)
    assert error.value.status_code==409
    from zcode_gateway import deps
    monkeypatch.setattr(deps,'get_state',lambda _:s)
    with pytest.raises(HTTPException) as error:await deps.client_key(None)
    assert error.value.status_code==503


@pytest.mark.asyncio
@pytest.mark.parametrize('version',['1.2.2','2.0.0','invalid'])
async def test_download_and_apply_reject_older_equal_or_invalid_release(monkeypatch,version):
    s=state()
    # Even a corrupted/stale update_available flag cannot authorize a downgrade.
    s.updater.status.update(latest=version,update_available=True)
    monkeypatch.setattr(api_dash,'get_state',lambda _:s)
    monkeypatch.setattr(api_dash,'_windows_update_only',lambda:None)
    for operation in (api_dash.update_download,api_dash.update_apply):
        with pytest.raises(HTTPException) as error:
            await operation(None,'admin')
        assert error.value.status_code==409
        assert 'downgrades are blocked' in error.value.detail
