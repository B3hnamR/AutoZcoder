"""Import a ZCode credentials file or the user's MITM dataset into this install."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gui_main import _bootstrap_config
from zcode_gateway.config import load_config
from zcode_gateway.zcode_accounts import import_credentials


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', help='credentials.json, tokens.json, or MITM dataset folder')
    parser.add_argument('--config')
    args = parser.parse_args()
    cfg = load_config(args.config or _bootstrap_config())
    source = Path(args.source)
    if source.is_dir():
        source = source / 'data' / 'tokens.json'
    result = import_credentials(source, cfg.accounts.dir)
    print(f"Imported {result['written']} account(s) into {cfg.accounts.dir}. Reload pool to activate.")
    return 0 if result['written'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
