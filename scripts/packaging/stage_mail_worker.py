"""Stage only distributable worker sources, never local deployment state."""

import argparse
from pathlib import Path
import shutil


WRANGLER_TEMPLATE = '''name = "zai-mail"
main = "src/index.js"
compatibility_date = "2025-01-01"

[[kv_namespaces]]
binding = "MAIL"
id = "00000000000000000000000000000000"
'''


def stage_mail_worker(source: Path, destination: Path) -> None:
    if source.is_symlink() or (source / "src").is_symlink():
        raise ValueError("Worker source directories must not be symlinks")
    files = [source / "package.json", source / "README.md"]
    scripts = sorted((source / "src").glob("*.js"))
    if source / "src" / "index.js" not in scripts:
        raise ValueError("Worker entry point src/index.js is missing")
    files.extend(scripts)
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Worker input must be a regular source file: {path}")

    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Worker staging directory must be empty")
    (destination / "src").mkdir()
    for path in files:
        shutil.copyfile(path, destination / path.relative_to(source))
    (destination / "wrangler.toml").write_bytes(WRANGLER_TEMPLATE.encode("utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    stage_mail_worker(args.source, args.destination)
