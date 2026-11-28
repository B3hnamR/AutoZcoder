"""Node.js tarball unpacking: the official layout passes, escapes do not."""
import io
import os
import tarfile

import pytest

from zcode_gateway.generator import _extract_node_archive


def _tar(path, members):
    with tarfile.open(path, "w:xz") as tf:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
            else:
                data = b"#!/bin/sh\n"
                info.size = len(data)
                info.mode = 0o4755          # setuid must be stripped
                tf.addfile(info, io.BytesIO(data))


def test_official_layout_with_relative_bin_links_extracts(tmp_path):
    archive = tmp_path / "node.tar.xz"
    _tar(archive, [
        ("node-v1/", "dir", ""), ("node-v1/bin/", "dir", ""),
        ("node-v1/lib/node_modules/npm/bin/npm-cli.js", "file", ""),
        ("node-v1/bin/node", "file", ""),
        # the member that Python 3.10.12's "data" filter wrongly rejects
        ("node-v1/bin/npm", "sym", "../lib/node_modules/npm/bin/npm-cli.js"),
    ])
    out = tmp_path / "out"
    out.mkdir()
    _extract_node_archive(archive, out)
    npm = out / "node-v1" / "bin" / "npm"
    assert npm.exists()
    if os.name != "nt":   # Windows without symlink rights gets a copy instead
        assert npm.is_symlink() and npm.resolve().name == "npm-cli.js"
        assert not (out / "node-v1" / "bin" / "node").stat().st_mode & 0o4000


@pytest.mark.parametrize("member", [
    ("../evil", "file", ""),
    ("/abs/evil", "file", ""),
    ("node-v1/bin/x", "sym", "/etc/passwd"),
    ("node-v1/bin/x", "sym", "../../../outside"),
])
def test_escaping_members_are_refused(tmp_path, member):
    archive = tmp_path / "bad.tar.xz"
    _tar(archive, [("node-v1/", "dir", ""), ("node-v1/bin/", "dir", ""), member])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError):
        _extract_node_archive(archive, out)
    assert not (tmp_path / "evil").exists()
