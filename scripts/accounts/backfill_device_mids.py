"""Give every account snapshot its own stable device_mid.

Dry run by default. Only accounts that share a mid with another account (or
have none) are touched; an account whose mid is already unique is left alone.
The new mid is `account_device_mid(account_id)`, the same derivation the
generator uses, so both sides agree after migration.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gui_main import _bootstrap_config
from zcode_gateway.accounts_dir import parse_snapshot, snapshot_transaction, write_snapshot
from zcode_gateway.config import load_config
from zcode_gateway.zcode_device import account_device_mid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', nargs='?',
                        help='accounts folder (default: the configured accounts.dir)')
    parser.add_argument('--config')
    parser.add_argument('--apply', action='store_true',
                        help='rewrite the files (default: dry run)')
    args = parser.parse_args()
    if args.directory:
        directory = Path(args.directory)
    else:
        directory = Path(load_config(args.config or _bootstrap_config()).accounts.dir)
    if not directory.is_dir():
        print(f"accounts folder not found: {directory}")
        return 1

    with snapshot_transaction(directory) as folder:
        rows = []
        for path in sorted(folder.iterdir()):
            if path.suffix.casefold() != '.txt' or not path.is_file() or path.is_symlink():
                continue
            try:
                fields = parse_snapshot(path)
            except Exception:
                print(f"skipping unreadable snapshot: {path.name}")
                continue
            ident = fields.get('account_id') or fields.get('zcode_user_id') or path.stem
            rows.append((path, fields, ident, fields.get('device_mid', '').strip()))

        shared = Counter(mid for *_, mid in rows if mid)
        targets = [row for row in rows if not row[3] or shared[row[3]] > 1]
        if not targets:
            print(f"every account in {directory} already has a unique device_mid")
            return 0
        print(f"{'Rewriting' if args.apply else 'Would rewrite'} "
              f"{len(targets)} of {len(rows)} account(s) in {directory}")
        for path, fields, ident, old in targets:
            new = account_device_mid(ident)
            print(f"{fields.get('email') or ident} -> {old or '(none)'} -> {new}")
            if not args.apply or new == old:
                continue
            fields['device_mid'] = new
            write_snapshot(path, fields)

    if not args.apply:
        print("Dry run: nothing written. Back up the accounts folder, "
              "then re-run with --apply.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
