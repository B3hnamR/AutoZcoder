"""PyInstaller loader isolation; dummy environment and subprocess fixtures only."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from zcode_gateway import process_env
from zcode_gateway import generator as gen


@pytest.mark.parametrize('original', [None, '', '/usr/local/customer-libs:/opt/vendor/lib'])
def test_frozen_linux_restores_loader_without_mutating_gateway(monkeypatch,original):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'platform','linux')
    monkeypatch.setenv('LD_LIBRARY_PATH','/tmp/_MEI-fixture:/wrong')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','/opt/app/pw-browsers')
    if original is None:monkeypatch.delenv('LD_LIBRARY_PATH_ORIG',raising=False)
    else:monkeypatch.setenv('LD_LIBRARY_PATH_ORIG',original)
    result=process_env.external_process_env({'ZAI_CAPTCHA_CMD':'/tmp/_MEI-fixture/tools/captcha-mint'})
    assert result.get('LD_LIBRARY_PATH')==(original or None)
    assert result.get('LD_LIBRARY_PATH_ORIG')==original
    assert process_env.external_process_env(result).get('LD_LIBRARY_PATH')==(original or None)
    assert result['PLAYWRIGHT_BROWSERS_PATH']=='/opt/app/pw-browsers'
    assert result['ZAI_CAPTCHA_CMD'].startswith('/tmp/_MEI-fixture/')
    assert os.environ['LD_LIBRARY_PATH']=='/tmp/_MEI-fixture:/wrong'


@pytest.mark.parametrize('platform,frozen',[('linux',False),('win32',True),('darwin',True)])
def test_other_launch_modes_preserve_environment(monkeypatch,platform,frozen):
    monkeypatch.setattr(sys,'platform',platform)
    monkeypatch.setattr(sys,'frozen',frozen,raising=False)
    monkeypatch.setenv('LD_LIBRARY_PATH','custom')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG','original')
    env=process_env.external_process_env({'CUSTOM':'value'})
    assert env['LD_LIBRARY_PATH']=='custom' and env['LD_LIBRARY_PATH_ORIG']=='original'
    assert env['CUSTOM']=='value'


def test_claim_and_signup_launches_do_not_inherit_bundled_libcrypto(tmp_path,monkeypatch):
    monkeypatch.setattr(gen,'resolve_python',lambda *a:'external-python')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'platform','linux')
    monkeypatch.setenv('LD_LIBRARY_PATH','/tmp/_MEI-fixture')
    monkeypatch.delenv('LD_LIBRARY_PATH_ORIG',raising=False)
    seen=[]
    def spawn(argv,**kwargs):
        seen.append(kwargs['env'])
        raise OSError('fixture: no process launched')
    monkeypatch.setattr(gen.subprocess,'Popen',spawn)
    worker=gen.GenWorker('fixture',['external-python','main.py'],
                         {'ZAI_OUTPUT_DIR':str(tmp_path),'ZAI_CAPTCHA_CMD':'/tmp/_MEI-fixture/mint'},tmp_path)
    worker.start()
    claim=gen.ClaimJob('claim','dummy@example.test','direct','',tmp_path,'external-python','dummy',data_dir=tmp_path,kind='start')
    claim.start()
    assert len(seen)==2
    assert all('LD_LIBRARY_PATH' not in env for env in seen)
    assert seen[0]['ZAI_CAPTCHA_CMD']=='/tmp/_MEI-fixture/mint'


@pytest.mark.skipif(os.name=='nt',reason='Linux external loader smoke')
def test_external_python_ssl_import_after_removing_poisoned_loader_path(tmp_path,monkeypatch):
    poison=tmp_path/'_MEI-fixture';poison.mkdir()
    (poison/'libcrypto.so.3').write_bytes(b'not an OpenSSL library')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('LD_LIBRARY_PATH',str(poison))
    monkeypatch.delenv('LD_LIBRARY_PATH_ORIG',raising=False)
    result=subprocess.run([sys.executable,'-I','-c','import ssl; print(ssl.OPENSSL_VERSION)'],
        env=process_env.external_process_env(),capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert 'OpenSSL' in result.stdout
