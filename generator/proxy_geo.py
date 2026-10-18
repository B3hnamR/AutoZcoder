"""Offline country / ASN lookup for proxy hosts (DB-IP Lite, CC BY 4.0).

Nothing is downloaded until ``download()`` is called. After that every lookup is
offline — no per-proxy API calls, no rate limits. If no database is present the
lookup simply returns empty values and the caller falls back to its own cache.

Files live in ``<output dir>/geo/`` so a portable install keeps them beside the
exe. Country database ~ 2 MB gz, ASN ~ 5 MB gz.
"""
from __future__ import annotations

import bisect
import csv
import gzip
import io
import ipaddress
import os
import re
import time
from array import array

COUNTRY_FILE = "dbip-country-lite.csv.gz"
ASN_FILE = "dbip-asn-lite.csv.gz"
DOWNLOAD_BASE = "https://download.db-ip.com/free/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

_data_dir = None
_country = None          # _Index
_asn = None              # _Index
_loaded_for = None

_COUNTRY_NAME = re.compile(r"[^A-Za-z0-9 .,'&()/-]")


class _Index:
    """Sorted IP ranges with a value per range (a 2-letter code or an ASN label)."""

    __slots__ = ("starts", "ends", "values")

    def __init__(self, starts: array, ends: array, values: list):
        self.starts, self.ends, self.values = starts, ends, values

    def __len__(self) -> int:
        return len(self.starts)

    @classmethod
    def from_rows(cls, rows) -> "_Index":
        starts, ends, values = array("I"), array("I"), []
        for start, end, value in rows:
            if not value:
                continue
            starts.append(start)
            ends.append(end)
            values.append(value)
        return cls(starts, ends, values)

    def lookup(self, ip_int: int) -> str:
        i = bisect.bisect_right(self.starts, ip_int) - 1
        if i >= 0 and ip_int <= self.ends[i]:
            return self.values[i]
        return ""


def set_data_dir(path) -> None:
    """Point the database folder at the pool's data directory."""
    global _data_dir, _country, _asn, _loaded_for
    new = str(path)
    if _data_dir == new and _loaded_for is not None:
        return                      # same folder: keep the parsed index
    _data_dir = new
    _country = _asn = None
    _loaded_for = None


def _dir() -> str:
    return os.path.join(_data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"), "geo")


def paths() -> dict:
    d = _dir()
    return {"dir": d, "country": os.path.join(d, COUNTRY_FILE), "asn": os.path.join(d, ASN_FILE)}


def available() -> bool:
    return os.path.exists(paths()["country"])


def status() -> dict:
    p = paths()
    out = {"available": os.path.exists(p["country"]), "dir": p["dir"],
           "country_db": os.path.exists(p["country"]), "asn_db": os.path.exists(p["asn"])}
    try:
        st = os.stat(p["country"])
        out["size_mb"] = round(st.st_size / 1e6, 1)
        out["updated"] = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
    except OSError:
        pass
    try:
        if _country is not None:
            out["ranges"] = len(_country)
    except Exception:
        pass
    return out


def ip_to_int(ip: str) -> int | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return int(addr)


def _iter_csv(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="ignore", newline="") as f:
        yield from csv.reader(f)


def _load_country(path: str) -> _Index:
    def rows():
        for row in _iter_csv(path):
            if len(row) < 3:
                continue
            start, end = ip_to_int(row[0]), ip_to_int(row[1])
            if start is None or end is None:
                continue
            if len(row) == 3:
                # current Lite CSV: ip_start, ip_end, country_code
                code, name = row[2], ""
            elif len(row) >= 6:
                # full/older layout: start, end, continent_code, continent_name,
                # country_code, eu_member, country_name
                code = row[4]
                name = row[6] if len(row) > 6 else ""
            else:
                code, name = row[2], (row[3] if len(row) > 3 else "")
            code = (code or "").strip().upper()
            name = _COUNTRY_NAME.sub("", name or "")[:48]
            yield start, end, (code + "|" + name) if code else ""
    return _Index.from_rows(rows())


def _load_asn(path: str) -> _Index:
    def rows():
        for row in _iter_csv(path):
            if len(row) < 4:
                continue
            start, end = ip_to_int(row[0]), ip_to_int(row[1])
            if start is None or end is None:
                continue
            org = (row[3] or "").strip()[:64]
            asn = (row[2] or "").strip()[:16]
            if org or asn:
                yield start, end, (asn + " " + org).strip()
    return _Index.from_rows(rows())


def _ensure_loaded() -> None:
    global _country, _asn, _loaded_for
    p = paths()
    key = (p["country"], p["asn"])
    if _loaded_for == key:
        return
    _country = _load_country(p["country"]) if os.path.exists(p["country"]) else None
    _asn = _load_asn(p["asn"]) if os.path.exists(p["asn"]) else None
    _loaded_for = key


def lookup(ip: str) -> dict:
    """{"country": "US", "country_name": "...", "asn": "...", "org": "..."} (empty when unknown)."""
    out = {"country": "", "country_name": "", "asn": "", "org": ""}
    n = ip_to_int(ip or "")
    if n is None:
        return out
    _ensure_loaded()
    if _country is not None:
        val = _country.lookup(n)
        if val:
            cc, _, name = val.partition("|")
            out["country"], out["country_name"] = cc, name
    if _asn is not None:
        val = _asn.lookup(n)
        if val:
            asn, _, org = val.partition(" ")
            out["asn"], out["org"] = asn, org
    return out


def country(ip: str) -> str:
    return lookup(ip)["country"]


def _download_one(kind: str, progress=None) -> dict:
    import urllib.request

    name = COUNTRY_FILE if kind == "country" else ASN_FILE
    os.makedirs(_dir(), exist_ok=True)
    target = os.path.join(_dir(), name)
    now = time.gmtime()
    candidates = []
    y, m = now.tm_year, now.tm_mon
    for _ in range(8):
        candidates.append(f"{DOWNLOAD_BASE}dbip-{kind}-lite-{y}-{m:02d}.csv.gz")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    last = ""
    for url in candidates:
        if progress:
            progress(f"trying {url.rsplit('/', 1)[-1]}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            if len(data) < 10_000:
                raise ValueError("file too small — not a database")
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                head = gz.read(200).decode("utf-8", "ignore")
            if "," not in head:
                raise ValueError("not a CSV database")
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            if progress:
                progress(f"saved {name} ({len(data)/1e6:.1f} MB)")
            return {"ok": True, "file": target, "bytes": len(data)}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    return {"ok": False, "error": last or "no database found"}


def download(progress=None, asn: bool = True) -> dict:
    """Fetch the current (or most recent) DB-IP Lite databases."""
    res = {"country": _download_one("country", progress)}
    if res["country"].get("ok") and asn:
        res["asn"] = _download_one("asn", progress)
    global _loaded_for
    _loaded_for = None
    res["status"] = status()
    res["ok"] = bool(res["country"].get("ok"))
    return res


def clear() -> None:
    global _country, _asn, _loaded_for
    for p in paths().values():
        if isinstance(p, str) and p.endswith((".gz",)) and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    _country = _asn = None
    _loaded_for = None
