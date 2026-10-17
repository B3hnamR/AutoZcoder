"""Check real quotas; --infer additionally makes one small model request."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zcode_gateway.config import load_config
from zcode_gateway.pool import load_accounts, PoolManager
from zcode_gateway.tokens import TokenManager
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService
from zcode_gateway.upstream import UpstreamClient
from zcode_gateway.store import Store, JsonlCapture


async def run(args):
    cfg = load_config(args.config)
    accounts = load_accounts(cfg.accounts)
    if not accounts:
        print('No accounts imported')
        return 1
    pool = PoolManager(cfg.pool, accounts)
    tokens = TokenManager(cfg, pool)
    client = UpstreamClient(cfg.upstream)
    try:
        data = await tokens.check_account(accounts[0])
        if data is None:
            print('Live quota check unavailable; credentials or network need checking')
            return 1
        print('Live quota check OK:', json.dumps(accounts[0].notes['quotas']))
        if args.infer:
            with tempfile.TemporaryDirectory() as folder:
                store = Store(str(Path(folder) / 'smoke.db'))
                try:
                    service = ChatService(cfg, pool, tokens, client, Registry(), store, JsonlCapture(folder, False))
                    model = 'GLM-5.3-Flash'
                    result = await service.complete({'model': model, 'messages': [{'role': 'user', 'content': 'Reply only with OK.'}],
                                                     'reasoning_effort': 'low', 'max_tokens': 256},
                                                    model=model, dialect='anthropic')
                    message = json.loads(result.raw)
                    print('Live inference OK:', message['stop_reason'], json.dumps(message['usage']))
                finally:
                    store.close()
        return 0
    except Exception as exc:
        print('Live check failed:', type(exc).__name__)
        if hasattr(exc, 'status'):
            import re
            message = re.sub(r'eyJ[\w.-]+', '<redacted>', str(exc))
            print('HTTP', exc.status, 'code', exc.code, message[:400])
        return 1
    finally:
        await tokens.aclose()
        await client.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--infer', action='store_true')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
