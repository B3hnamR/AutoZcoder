"""Browser approval through ZCode's captured CLI OAuth broker."""
import asyncio
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse, quote
import httpx
from .accounts_dir import snapshot_filename, parse_snapshot, write_snapshot
from .config import Fingerprint
from .pool import Account
from .upstream import build_headers
from .zcode_detect import jwt_claims

class DeviceLogin:
    def __init__(self, accounts_dir: Path, client=None, on_account=None, base_url='https://zcode.z.ai/api/v1', fingerprint=None):
        self.accounts_dir, self.on_account = Path(accounts_dir), on_account
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self.base_url, self.fingerprint = base_url.rstrip('/'), fingerprint or Fingerprint()
        self.state, self.error = 'idle', None
        self.user_code, self.verification_url, self.account_email, self.snapshot_path = None, None, None, None
        self._ready, self._cancel = asyncio.Event(), asyncio.Event()
        self._poll_token, self._flow_id = None, None
        self._expires_at, self._interval = 0, 2

    def status(self):
        return {'state': self.state, 'user_code': self.user_code, 'verification_url': self.verification_url,
                'error': self.error, 'email': self.account_email, 'expires_at': self._expires_at}

    def cancel(self): self._cancel.set()

    async def _authorize(self):
        self._poll_token = secrets.token_hex(32)
        headers = build_headers(self._poll_token, self.fingerprint)
        headers.pop('x-api-key', None)
        resp = await self.client.post(self.base_url+'/oauth/cli/init', json={'provider': 'zai'},
                                      headers=headers)
        resp.raise_for_status()
        envelope = resp.json()
        data = envelope.get('data') or {}
        if envelope.get('code') != 0 or not all(data.get(k) for k in ('flow_id', 'poll_token', 'authorize_url')):
            raise ValueError('ZCode did not return a valid login flow')
        parsed = urlparse(data['authorize_url'])
        if parsed.scheme != 'https' or parsed.hostname not in ('chat.z.ai', 'zcode.z.ai'):
            raise ValueError('ZCode returned an unexpected login URL')
        self._flow_id, self._poll_token = data['flow_id'], data['poll_token']
        self.verification_url = data['authorize_url']
        self._expires_at = data.get('expires_at') or time.time()+300
        self._interval = max(1, min(30, data.get('poll_interval_sec') or 2))
        return True

    async def _poll(self):
        while time.time() < self._expires_at:
            if self._cancel.is_set():
                self.state = 'cancelled'
                return None
            headers = build_headers(self._poll_token, self.fingerprint)
            headers.pop('x-api-key', None)
            resp = await self.client.get(self.base_url+'/oauth/cli/poll/'+quote(self._flow_id, safe=''), headers=headers)
            resp.raise_for_status()
            envelope = resp.json()
            data = envelope.get('data') or {}
            if envelope.get('code') != 0:
                raise ValueError('ZCode rejected the login poll')
            status = data.get('status')
            if status == 'ready' and data.get('token'): return data
            if status in ('expired', 'cancelled', 'denied', 'error', 'failed'):
                self.state = 'expired' if status == 'expired' else 'error'
                self.error = 'Login expired' if status == 'expired' else 'Login was not approved'
                return None
            try: await asyncio.wait_for(self._cancel.wait(), timeout=self._interval)
            except asyncio.TimeoutError: pass
        self.state, self.error = 'expired', 'Login expired; start again'
        return None

    async def _register(self, data):
        token, user = data['token'], data.get('user') or {}
        claims = jwt_claims(token)
        ident = user.get('user_id') or claims.get('user_id') or claims.get('sub')
        if not ident: raise ValueError('ZCode login returned no account identity')
        account = Account(id=ident, email=user.get('email', ''), access_token=token,
                          expires_at=int(claims.get('exp') or 0)*1000, source='device-login',
                          notes={'name': user.get('name', ''), 'device_mid': self.fingerprint.device_mid})
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        used = {p.name: parse_snapshot(p).get('account_id') for p in self.accounts_dir.glob('*.txt')}
        name = snapshot_filename(account.email, ident, used)
        self.snapshot_path = self.accounts_dir / name
        await asyncio.to_thread(write_snapshot, self.snapshot_path,
            {'account_id': ident, 'email': account.email, 'name': user.get('name', ''),
             'access_token': token, 'expires_at_ms': str(account.expires_at),
             'device_mid': self.fingerprint.device_mid, 'source': 'ZCode CLI OAuth'})
        account.notes['file'] = name
        self.account_email = account.email
        return account

    async def run(self):
        try:
            await self._authorize()
            self.state = 'awaiting_user'
            self._ready.set()
            data = await self._poll()
            if data is None: return
            if self._cancel.is_set():
                self.state = 'cancelled'
                return
            self.state = 'registering'
            account = await self._register(data)
            if self.on_account: await self.on_account(account)
            self.state = 'done'
        except asyncio.CancelledError:
            self.state = 'cancelled'
        except Exception as exc:
            self.state, self.error = 'error', f'Login failed ({type(exc).__name__})'
        finally:
            self._ready.set()
            self._poll_token = None
            if self._own_client: await self.client.aclose()
