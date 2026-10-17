"""Import ZCode JWTs without copying chat, business or API-key credentials."""
import hashlib
import base64
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .accounts_dir import parse_snapshot, snapshot_filename, write_snapshot
from .zcode_detect import jwt_claims
from .zcode_device import read_device_mid


def decrypt_credential(value):
    if not isinstance(value, str) or not value.startswith('enc:v1:'):
        return value
    from .credential_cipher import decrypt_aes_gcm
    parts = value[len('enc:v1:'):].split('.')
    if len(parts) != 3:
        raise ValueError('Invalid ZCode credential format')
    try:
        iv, tag, ciphertext = [base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)) for p in parts]
        platform = 'win32' if sys.platform == 'win32' else sys.platform
        secret = os.environ.get('ZCODE_CREDENTIAL_SECRET', '').strip()
        secret = secret or f'zcode-credential-fallback:{platform}:{Path.home()}:{getpass.getuser()}'
        key = hashlib.sha256(secret.encode()).digest()
        return decrypt_aes_gcm(key, iv, ciphertext, tag).decode('utf-8')
    except Exception as exc:
        raise ValueError('Cannot decrypt ZCode credentials; use Login with Zcode or set ZCODE_CREDENTIAL_SECRET') from exc


def read_credentials(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    device_mid = read_device_mid(Path(path).parent / 'telemetry-state.json')
    token = decrypt_credential(data.get('zcodejwttoken') or data.get('zcodeJWT'))
    user = decrypt_credential(data.get('oauth:zai:user_info')) or data.get('user') or {}
    if isinstance(user, str):
        try:
            user = json.loads(user)
        except ValueError:
            user = {}
    current_claims = jwt_claims(token) if token else {}
    current_id = current_claims.get('user_id') or current_claims.get('sub')
    candidates = [token] if token else []
    candidates.extend(decrypt_credential(value) for key, value in data.items()
                      if key.startswith('account-provider:coding-plan:account:') and key.endswith(':api-key'))
    accounts = {}
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        claims = jwt_claims(candidate)
        # Exclude API keys and other token types. The provider verifies signatures.
        ident = claims.get('user_id') or claims.get('sub')
        if not ident or candidate.count('.') != 2:
            continue
        account_user = user if ident == current_id else {}
        fields = {'account_id': ident, 'email': account_user.get('email', ''), 'name': account_user.get('name', ''),
                  'access_token': candidate, 'expires_at_ms': str(int(claims.get('exp') or 0) * 1000),
                  'source': str(path), 'captured_at': datetime.now(timezone.utc).isoformat()}
        if device_mid:
            fields['device_mid'] = device_mid
        if ident not in accounts or int(claims.get('iat') or 0) > int(jwt_claims(accounts[ident]['access_token']).get('iat') or 0):
            accounts[ident] = fields
    return list(accounts.values())


def import_credentials(path, output_dir):
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    used = {p.name: parse_snapshot(p).get('account_id') for p in folder.glob('*.txt')}
    known = set(used.values())
    files, new_ids, emails = [], [], []
    for fields in read_credentials(path):
        ident = fields['account_id']
        name = snapshot_filename(fields['email'], ident, used)
        used[name] = ident
        write_snapshot(folder / name, fields)
        files.append(name)
        if ident not in known:
            new_ids.append(ident)
        if fields['email']:
            emails.append(fields['email'])
    return {'written': len(files), 'files': files, 'new_ids': new_ids,
            'emails': emails, 'updated': len(files) - len(new_ids)}
