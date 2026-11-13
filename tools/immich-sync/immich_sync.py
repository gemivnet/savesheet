# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "requests-toolbelt", "pyyaml", "exifread"]
# ///
"""Keep Immich and G:\\Files in step, both ways: upload missing photos, put them in folder
albums, repair dates that are missing or untrustworthy - and make sure everything in
Immich also has a source copy in G:\\Files, so the Files backups (external drive, Google
Drive, Glacier) can rebuild Immich if the NAS is lost.

    uv run immich_sync.py scan                 hash + read dates (cached in state.sqlite)
    uv run immich_sync.py sync [--dry-run]     upload missing files, add to albums
    uv run immich_sync.py fix-dates [--dry-run]  set inferred dates + Date/* tags
    uv run immich_sync.py backfill [--dry-run] download Immich originals that Files lacks
    uv run immich_sync.py db-backup [--dry-run]  copy Immich's newest nightly database dump
    uv run immich_sync.py report               suspicious date clusters in Immich
    uv run immich_sync.py all [--dry-run]      scan, sync, fix-dates, backfill, db-backup, report

Dates are only ever changed in Immich, never in the files. Every inferred date gets
a Date/* tag. An asset is dated once; later runs leave it alone so manual fixes stick.

The code lives in the savesheet repo (tools/immich-sync); savesheet's wrapper copies it
here next to rules.yaml / state.sqlite / reports before each run, so edit it there.
"""
import argparse
import base64
import calendar
import collections
import csv
import datetime as dt
import fnmatch
import hashlib
import html
import json
import os
import gzip
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import exifread
import requests
import yaml
from requests_toolbelt import MultipartEncoder

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
NOW = dt.datetime.now()

logging_exifread = __import__("logging").getLogger("exifread")
logging_exifread.setLevel(50)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- config / api

def load_rules():
    r = yaml.safe_load((HERE / "rules.yaml").read_text(encoding="utf-8"))
    r["image_ext"] = set(r["image_ext"]) - set(r.get("skip_ext") or [])
    r["video_ext"] = set(r["video_ext"]) - set(r.get("skip_ext") or [])
    # Defaults for the Immich -> Files direction, overridable in rules.yaml. backfill_dir
    # must sit under an `include` folder, or the next scan won't see what it downloaded
    # and every run would fetch the same files again.
    r.setdefault("backfill_dir", "Photos/From Immich")
    r.setdefault("db_backup_dir", "Server Backup/immich-db")
    r.setdefault("backfill_min_free_gb", 50)   # never fill G: past this much free space
    if not any(in_dir(r["backfill_dir"], t) for t in r["include"]):
        raise SystemExit(f"rules.yaml: backfill_dir {r['backfill_dir']!r} is not under any `include` folder")
    return r


def in_dir(rel, folder):
    return rel.lower().startswith(folder.lower().rstrip("/") + "/")


def in_backfill(rules, rel):
    return in_dir(rel, rules["backfill_dir"])


def match(rel, patterns):
    rel = rel.lower()
    return any(fnmatch.fnmatchcase(rel, p.lower()) for p in patterns)


def lookup(rel, mapping):
    """Value for the longest pattern in `mapping` matching rel, else None."""
    best = None
    for p, v in mapping.items():
        if match(rel, [p]) and (best is None or len(p) > len(best[0])):
            best = (p, v)
    return best[1] if best else None


class Immich:
    def __init__(self, auth="~/.config/immich/auth.yml"):
        cfg = Path(auth).expanduser().read_text()
        self.url = re.search(r"url:\s*(\S+)", cfg).group(1).rstrip("/")
        self.s = requests.Session()
        self.s.headers["x-api-key"] = re.search(r"key:\s*(\S+)", cfg).group(1)

    def call(self, method, path, **kw):
        for attempt in range(5):
            try:
                r = self.s.request(method, self.url + path, timeout=600, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
                if r.status_code >= 400:
                    raise SystemExit(f"{method} {path} -> {r.status_code} {r.text[:500]}")
                return r.json() if r.content else None
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if attempt == 4:
                    raise
                log(f"retry {method} {path}: {e}")
                time.sleep(5 * (attempt + 1))

    def all_assets(self):
        out, page = [], 1
        while page:
            res = self.call("POST", "/search/metadata",
                            json={"page": page, "size": 1000, "withExif": True})["assets"]
            out += res["items"]
            page = int(res["nextPage"]) if res.get("nextPage") else None
        return out

    def every_asset(self):
        """Every asset this key can see (its own and partner-shared), including the hidden
        halves of Live Photos, which the default search leaves out. Locked-folder assets
        need a PIN-elevated session that an API key cannot have, so they are not listed.
        Uses the cursor paging that replaced page numbers in Immich v3.2."""
        out, cursor = [], None
        while True:
            body = {"size": 1000, "withExif": True,
                    "filter": {"visibility": {"in": ["timeline", "archive", "hidden"]}}}
            if cursor:
                body["cursor"] = cursor
            res = self.call("POST", "/search/metadata", json=body)["assets"]
            out += res["items"]
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def download(self, path, dest, expect_sha=None):
        """Stream GET `path` to `dest` via a .part file; True once it is in place. With
        expect_sha (base64 SHA-1, Immich's checksum format) a mismatch is discarded."""
        part = dest.with_name(dest.name + ".part")
        for attempt in range(3):
            try:
                h = hashlib.sha1()
                with self.s.get(self.url + path, stream=True, timeout=600) as r:
                    if 400 <= r.status_code < 500:   # missing / forbidden: retrying won't help
                        log(f"download failed {path} -> {dest.name}: {r.status_code} {r.text[:200]}")
                        return False
                    if r.status_code >= 500:
                        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                            h.update(chunk)
                if expect_sha and base64.b64encode(h.digest()).decode() != expect_sha:
                    raise RuntimeError("checksum mismatch")
                os.replace(part, dest)
                return True
            except (requests.RequestException, RuntimeError, OSError) as e:
                part.unlink(missing_ok=True)
                if attempt == 2:
                    log(f"download failed {path} -> {dest.name}: {e}")
                    return False
                time.sleep(5 * (attempt + 1))

    def wait_for_queues(self, max_waiting):
        """Uploading faster than the server thumbnails/ML-processes took it down once
        (2026-10-30, 4 parallel uploads of 11 MB scans). Pause until the backlog drains."""
        heavy = ("thumbnailGeneration", "metadataExtraction", "faceDetection",
                 "facialRecognition", "smartSearch", "videoConversion")
        while True:
            try:
                jobs = self.call("GET", "/jobs") or {}
            except Exception as e:
                log(f"queue check failed ({e}); pausing 2 minutes")
                time.sleep(120)
                continue
            # A queue paused on purpose (e.g. transcoding during a big import) never drains.
            backlog = max([((jobs.get(q) or {}).get("jobCounts") or {}).get("waiting", 0) for q in heavy
                           if not ((jobs.get(q) or {}).get("queueStatus") or {}).get("isPaused")] or [0])
            if backlog <= max_waiting:
                return
            log(f"server busy ({backlog} jobs queued); pausing")
            time.sleep(60)

    def wait_for_metadata(self):
        """Uploaded files are dated by the metadata job; let it finish before we override."""
        for path in ("/jobs", "/queues"):
            try:
                r = self.s.get(self.url + path, timeout=60)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            while True:
                q = r.json().get("metadataExtraction", {})
                c = q.get("jobCounts") or q.get("statistics") or {}
                busy = (c.get("active") or 0) + (c.get("waiting") or 0)
                if not busy:
                    return
                log(f"waiting for metadata extraction ({busy} queued)")
                time.sleep(30)
                r = self.s.get(self.url + path, timeout=60)
        log("job queue status unavailable; waiting 2 minutes")
        time.sleep(120)


# ---------------------------------------------------------------- state

def open_state():
    db = sqlite3.connect(HERE / "state.sqlite")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY, size INT, mtime REAL, sha1 TEXT,
            capture TEXT, capture_read INT DEFAULT 0, asset_id TEXT);
        CREATE TABLE IF NOT EXISTS dated(
            asset_id TEXT PRIMARY KEY, path TEXT, rule TEXT, date TEXT, at TEXT, old TEXT);
        CREATE TABLE IF NOT EXISTS albumed(asset_id TEXT PRIMARY KEY, album TEXT);
    """)
    return db


# ---------------------------------------------------------------- scan

def walk(rules):
    root = Path(rules["root"])
    for top in rules["include"]:
        for dirpath, dirs, files in os.walk(root / top):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            if (match(rel_dir + "/", rules["exclude"])
                    and not match(rel_dir + "/", rules.get("exclude_except", []))):
                dirs[:] = []
                continue
            for f in files:
                if f.startswith(".") and not f.startswith(tuple(rules.get("hidden_file_prefixes", []))):
                    continue  # AppleDouble etc.
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if ext in rules["image_ext"] or ext in rules["video_ext"]:
                    yield f"{rel_dir}/{f}"


def sha1_b64(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return base64.b64encode(h.digest()).decode()


def read_capture(path, is_video):
    """Capture timestamp from EXIF / QuickTime, as 'YYYY-MM-DDTHH:MM:SS', or ''."""
    try:
        if is_video:
            out = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                 "format_tags=creation_time", str(path)],
                capture_output=True, text=True, timeout=60).stdout
            t = (json.loads(out or "{}").get("format", {}).get("tags", {}) or {}).get("creation_time", "")
            return t[:19] if t else ""
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False)
        for k in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized"):
            v = str(tags.get(k, "")).strip()
            m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", v)
            if m and m.group(1) != "0000":
                return "{}-{}-{}T{}:{}:{}".format(*m.groups())
    except Exception:
        pass
    return ""


def scan(rules, db):
    root = Path(rules["root"])
    paths = list(walk(rules))
    log(f"scan: {len(paths)} candidate files")
    known = {p: (s, m, h, cr) for p, s, m, h, cr in
             db.execute("SELECT path, size, mtime, sha1, capture_read FROM files")}
    todo = []
    for rel in paths:
        st = (root / rel).stat()
        k = known.get(rel)
        if not k or k[0] != st.st_size or abs(k[1] - st.st_mtime) > 2 or not k[2] or not k[3]:
            todo.append((rel, st.st_size, st.st_mtime, k[2] if k and k[0] == st.st_size and abs(k[1] - st.st_mtime) <= 2 else None))
    log(f"scan: {len(todo)} new or changed files to hash/read")

    def work(item):
        rel, size, mtime, sha = item
        p = root / rel
        is_video = rel.rsplit(".", 1)[-1].lower() in rules["video_ext"]
        try:
            return rel, size, mtime, sha or sha1_b64(p), read_capture(p, is_video)
        except OSError as e:
            log(f"unreadable {rel}: {e}")
            return None

    done = 0
    with ThreadPoolExecutor(8) as ex:
        for r in ex.map(work, todo):
            if r:
                db.execute("""INSERT INTO files(path,size,mtime,sha1,capture,capture_read)
                              VALUES(?,?,?,?,?,1) ON CONFLICT(path) DO UPDATE SET
                              size=excluded.size, mtime=excluded.mtime, sha1=excluded.sha1,
                              capture=excluded.capture, capture_read=1""", r)
            done += 1
            if done % 5000 == 0:
                db.commit()
                log(f"scan: {done}/{len(todo)}")
    present = set(paths)
    gone = [p for p in known if p not in present]
    db.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
    db.commit()
    return paths


# ---------------------------------------------------------------- date sources

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
FB_DATE = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?\s?([AaPp][Mm])")
SRC = re.compile(r'<(?:img|video)[^>]*?\bsrc="([^"]+)"|<a href="([^"]+\.(?:jpe?g|png|gif|webp|heic|mp4|mov))"', re.I)


def meta_export_dates(rules):
    """Facebook / Instagram HTML exports: the timestamp printed after each photo."""
    root = Path(rules["root"])
    dates = {}
    # Each export root is the folder the html srcs are relative to.
    fb = root / "Shelby Documents/Facebook"
    exports = [p for p in fb.glob("*") if p.is_dir()] + [root / "Shelby Documents/Instagram"]
    for export in exports:
        if not export.is_dir():
            continue
        for h in export.rglob("*.html"):
            try:
                text = h.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in SRC.finditer(text):
                src = html.unescape(m.group(1) or m.group(2))
                if src.startswith("http"):
                    continue
                # A post prints its date once, after all of its (sometimes dozens of) photos.
                d = FB_DATE.search(text, m.end(), m.end() + 100000)
                if not d:
                    continue
                mon, day, year, hh, mm, ss, ap = d.groups()
                hh = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
                try:
                    when = dt.datetime(int(year), MONTHS[mon.lower()], int(day), hh, int(mm), int(ss or 0))
                except (KeyError, ValueError):
                    continue
                for base in (export, h.parent):
                    f = (base / src).resolve()
                    if f.exists():
                        rel = f.relative_to(root.resolve()).as_posix()
                        dates.setdefault(rel, when)
                        break
    return dates


def takeout_date(root, rel, _cache={}):
    """Google Takeout sidecar json (handles truncated / .supplemental-metadata names)."""
    p = root / rel
    d = p.parent
    if d not in _cache:
        _cache[d] = [j.name for j in d.glob("*.json")] if d.exists() else []
    jsons = _cache[d]
    if not jsons:
        return None
    name = p.name
    stem_n = re.sub(r"\((\d+)\)(\.[^.]+)$", r"\2", name)  # IMG(1).jpg -> IMG.jpg(1).json
    cands = [j for j in jsons if j in (name + ".json", name + ".supplemental-metadata.json")]
    if not cands:
        for j in jsons:
            base = re.sub(r"\(\d+\)$", "", j[:-5])
            base = re.sub(r"\.su[a-z-]*$", "", base)
            if len(base) >= 20 and stem_n.startswith(base):
                cands.append(j)
    for j in cands:
        try:
            meta = json.loads((d / j).read_text(encoding="utf-8"))
            ts = int(meta["photoTakenTime"]["timestamp"])
            # Google stores the EXIF wall-clock time as if it were UTC.
            return dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None)
        except Exception:
            continue
    return None


def moi_index(rules):
    """JVC camcorder .MOI sidecars, which hold each .MOD clip's recording time. Copies of
    the clips and their sidecars often ended up in different folder trees, so they are
    matched by clip name (MOV08D), preferring a sidecar in a folder of the same name."""
    idx = collections.defaultdict(list)
    for top in rules["include"]:
        for p in (Path(rules["root"]) / top).rglob("*.[mM][oO][iI]"):
            try:
                b = p.read_bytes()[:14]
                when = dt.datetime(int.from_bytes(b[6:8], "big"), b[8], b[9], b[10], b[11],
                                   int.from_bytes(b[12:14], "big") // 1000)
            except (OSError, ValueError):
                continue
            idx[p.stem.upper()].append((p.parent.name.lower(), when))
    return idx


DATE_IN_NAME = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?(?!\d)")
D = r"((?:19|20)\d{2})(?:-(\d{1,2})(?:-(\d{1,2}))?)?"
RANGE = re.compile(rf"(?<!\d){D}\s*(?:to|through|-|–)\s*{D}(?!\d)", re.I)
SINGLE = re.compile(rf"(?<![\dx-]){D}(?![\dx])", re.I)  # not 1920x1080
MDY = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)")
MY = re.compile(r"(?<![\d-])(\d{1,2})-(\d{4}|\d{2})(?![\d-])")              # 11-09 pix, 6-2011
MON_Y = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*'?(\d{4}|\d{2})(?!\d)", re.I)


def _year(y):
    y = int(y)
    return y if y > 100 else (2000 + y if y < 40 else 1900 + y)


def _period(y, m, d):
    y = int(y)
    if m and d:
        s = dt.datetime(y, int(m), int(d))
        return s, s + dt.timedelta(days=1)
    if m:
        s = dt.datetime(y, int(m), 1)
        return s, dt.datetime(y + (int(m) == 12), int(m) % 12 + 1, 1)
    return dt.datetime(y, 1, 1), dt.datetime(y + 1, 1, 1)


def holiday(text, y):
    """'Easter 1987', 'Christmas 2005': the holiday's date in that year, else None."""
    t = text.lower()
    if "easter" in t:  # Meeus/Jones/Butcher Gregorian algorithm
        a, b, c = y % 19, y // 100, y % 100
        d, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        n = h + l - 7 * m + 114
        return dt.datetime(y, n // 31, n % 31 + 1)
    if "thanksgiving" in t or "thxgvng" in t:
        nov1 = dt.datetime(y, 11, 1)
        return nov1 + dt.timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    for words, md in ((("christmas", "xmas"), (12, 25)), (("halloween",), (10, 31)),
                      (("new year's eve", "nye"), (12, 31)), (("4th of july", "fourth of july", "july 4"), (7, 4)),
                      (("valentine",), (2, 14))):
        if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
            return dt.datetime(y, *md)
    return None


def name_period(text):
    """(start, end) of a date or date range written in a folder/file name, or None."""
    try:
        m = RANGE.search(text)
        if m:
            a, b = _period(*m.groups()[:3]), _period(*m.groups()[3:])
            if a[0] < b[1]:
                return a[0], b[1]
        m = MON_Y.search(text)  # before SINGLE: "May 2008" is May, not all of 2008
        if m:
            return _period(_year(m.group(2)), MONTHS[m.group(1).lower()], None)
        m = SINGLE.search(text)
        if m:
            y, mo, d = m.groups()
            h = not mo and holiday(text, int(y))
            if h:
                return h, h + dt.timedelta(days=1)
            return _period(y, mo, d)
        m = MDY.search(text)
        if m:
            mo, dd, yy = m.groups()
            return _period(_year(yy), mo, dd)
        m = MY.search(text)
        if m and 1 <= int(m.group(1)) <= 12:
            return _period(_year(m.group(2)), m.group(1), None)
    except ValueError:
        pass
    return None


def period_midpoint(text):
    """Midpoint of a date or date range written in a folder/file name, at noon."""
    p = name_period(text)
    if not p:
        return None
    return (p[0] + (p[1] - p[0]) / 2).replace(hour=12, minute=0, second=0, microsecond=0)


def plausible(d, min_year=1826):
    return d is not None and min_year <= d.year and d <= NOW + dt.timedelta(days=1)


Y2K = dt.datetime(2000, 1, 1)


def bad_capture(s):
    """Camera-clock defaults and epoch placeholders. A reset clock's 2000-01-01 00:00 also
    shows up as 1999-12-31 evening once a timezone is applied, in EXIF and in file mtimes."""
    if not s or s[:4] in ("1970", "1980", "1904") or s < "1990" or s > NOW.isoformat():
        return True
    try:
        return abs(dt.datetime.fromisoformat(s[:19]) - Y2K) < dt.timedelta(days=1)
    except ValueError:
        return False


class DateResolver:
    def __init__(self, rules, rows):
        self.rules = rules
        self.root = Path(rules["root"])
        log("dates: reading Facebook/Instagram export html")
        self.meta = meta_export_dates(rules)
        log(f"dates: {len(self.meta)} export-dated files")
        self.moi = moi_index(rules)
        # camera-default clusters and bulk-copy mtimes, per folder
        caps, days = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
        for path, _, mtime, _, capture in rows:
            folder = path.rsplit("/", 1)[0]
            if capture:
                caps[folder][capture] += 1
            days[folder][dt.datetime.fromtimestamp(mtime).date()] += 1
        n = rules["exif_cluster_min"]
        self.bad_caps = {(f, c) for f, cnt in caps.items() for c, k in cnt.items() if k >= n}
        self.bulk_days = {}
        for f, cnt in days.items():
            total = sum(cnt.values())
            day, k = cnt.most_common(1)[0]
            if total >= 10 and k / total >= 0.9:
                self.bulk_days[f] = day

    def within(self, folder, mtime, start, end):
        """The file's modified time, if it falls inside a known date/period: a real time of
        day instead of the noon placeholder."""
        m = dt.datetime.fromtimestamp(mtime)
        if (start <= m < end and self.bulk_days.get(folder) != m.date()
                and not bad_capture(m.isoformat())):
            return m
        return None

    def resolve(self, rel, mtime, capture):
        """-> (datetime or None to keep the capture date, rule)."""
        if in_backfill(self.rules, rel):
            # A copy downloaded from Immich: Immich's date is the source of truth, and the
            # folder it sits in was named from that date - never re-date from it.
            return None, "immich-original"
        r, folder = self.rules, rel.rsplit("/", 1)[0]
        name = rel.rsplit("/", 1)[1]
        # 0. camcorder sidecar
        if name.lower().endswith(".mod"):
            stem = re.sub(r"\s*\(\d+\)$", "", name.rsplit(".", 1)[0]).upper()  # MOV001 (2)
            cands = self.moi.get(stem, [])
            same = [w for f, w in cands if f == folder.rsplit("/", 1)[-1].lower()]
            if same or len({w for _, w in cands}) == 1:
                return (same or [cands[0][1]])[0], "Date/From-Camera-Sidecar"
        # 1. export metadata
        d = self.meta.get(rel) or takeout_date(self.root, rel)
        if plausible(d) and not bad_capture(d.isoformat()):
            return d, "Date/From-Export"
        # 2. trustworthy capture date: keep what Immich read
        if (capture and not match(rel, r["exif_untrusted"]) and not bad_capture(capture)
                and (folder, capture) not in self.bad_caps):
            return None, "exif"
        # 3. filename
        m = DATE_IN_NAME.search(name)
        if m:
            try:
                y, mo, da, hh, mi, ss = m.groups()
                d = dt.datetime(int(y), int(mo), int(da), int(hh or 12), int(mi or 0), int(ss or 0))
                if plausible(d, 1900):
                    if hh is None:
                        d = self.within(folder, mtime, d.replace(hour=0), d.replace(hour=0) + dt.timedelta(days=1)) or d
                    return d, "Date/From-Filename"
            except ValueError:
                pass
        stem = name.rsplit(".", 1)[0]
        if re.match(r"(19|20)\d{2}", stem):
            p = name_period(stem)
            d = period_midpoint(stem)
            if plausible(d, 1900):
                return self.within(folder, mtime, *p) or d, "Date/From-Filename"
        # 4. folder names, innermost first
        parts = folder.split("/")
        for i in range(len(parts), 0, -1):
            if match(parts[i - 1], r["folder_date_ignore"]):
                continue
            p = name_period(parts[i - 1])
            d = period_midpoint(parts[i - 1])
            if plausible(d, 1900):
                return self.within(folder, mtime, *p) or d, "Date/From-Folder"
        # 5. modified time where it survived copying
        cutoff = lookup(rel, r["mtime_trusted"])
        if cutoff:
            m = dt.datetime.fromtimestamp(mtime)
            if (m < dt.datetime.fromisoformat(str(cutoff)) and self.bulk_days.get(folder) != m.date()
                    and plausible(m, 1990) and not bad_capture(m.isoformat())):
                return m, "Date/From-Modified"
        # 6/7. unknown
        if match(rel, r["unknown_scan_globs"]):
            return dt.datetime.fromisoformat(r["unknown_scan_date"]).replace(hour=12), "Date/Unknown-Scan"
        return dt.datetime.fromisoformat(r["unknown_modern_date"]).replace(hour=12), "Date/Unknown-Modern"


def album_name(rules, rel):
    if in_backfill(rules, rel):
        return None
    depth = lookup(rel, rules["album_depth"])
    if not depth and match(rel, rules["no_album"]):
        return None
    parts = rel.split("/")[:-1]
    if depth:
        parts = parts[:depth]
    parts = parts[:1] + [p for p in parts[1:] if not match(p, rules["album_noise"])]
    return " / ".join(parts) if len(parts) > 1 else None


def norm_name(n):
    # "Copy (2) of P1010018 (2)(16).JPG", "IMG_1 (2013_01_24 UTC).jpg" -> "p1010018.jpg", "img_1.jpg"
    n = n.lower()
    n = re.sub(r"^copy( \(\d+\))? of ", "", n)
    stem, _, ext = n.rpartition(".")
    stem = re.sub(r"\s*\([^)]*\)", "", stem)
    stem = re.sub(r"-(edited|bearbeitet)$", "", stem)
    return f"{stem.strip()}.{ext}"


# ---------------------------------------------------------------- commands

def build_plan(rules, db, clients):
    """clients: {owner id: Immich}, uploading account first."""
    api = next(iter(clients.values()))
    rows = [r for r in db.execute("SELECT path, size, mtime, sha1, capture FROM files")]
    log("immich: fetching asset index")
    assets = api.all_assets()
    by_sum = {a["checksum"]: a for a in assets}
    # Probable-duplicate check compares capture days. Include each asset's pre-fix date,
    # or re-dating an asset would make its Takeout copies look new on the next run.
    old = dict(db.execute("SELECT asset_id, old FROM dated"))
    by_name = collections.defaultdict(list)
    for a in assets:
        days = by_name[norm_name(a["originalFileName"])]
        days.append(a["localDateTime"][:10])
        if old.get(a["id"]):
            days.append(old[a["id"]][:10])
    log(f"immich: {len(assets)} assets")
    # Undated photos borrow the date of a visually identical copy (Immich duplicate
    # detection) that has a real one, e.g. a Takeout re-encode of a dated disc original.
    by_id = {a["id"]: a for a in assets}
    placeholder = {aid for aid, rule in db.execute("SELECT asset_id, rule FROM dated")
                   if rule.startswith("Date/Unknown")}
    twin_date = {}
    for c in clients.values():
        for g in c.call("GET", "/duplicates") or []:
            real = [by_id.get(x["id"], x) for x in g["assets"]
                    if x["id"] not in placeholder and not bad_capture(x["localDateTime"][:19])
                    and x["localDateTime"][:4] not in ("1900", "1905")]
            real = [x for x in real if x["localDateTime"][11:19] != "12:00:00"] or real
            if real:
                times = sorted(dt.datetime.fromisoformat(x["localDateTime"][:19]) for x in real)
                for x in g["assets"]:
                    twin_date.setdefault(x["id"], times)
    res = DateResolver(rules, rows)
    plan = []
    for path, size, mtime, sha, capture in rows:
        ext = path.rsplit(".", 1)[-1].lower()
        is_video = ext in rules["video_ext"]
        date, rule = res.resolve(path, mtime, capture)
        a = by_sum.get(sha)
        if a and a.get("ownerId") not in clients:
            date, rule = None, "partner"  # shared with us, but no key for its owner
        twins = twin_date.get(a["id"], []) if a else []
        if twins and rule.startswith("Date/Unknown"):
            date, rule = twins[0], "Date/From-Duplicate"
        elif twins and date and date.time() == dt.time(12):
            # Noon is the "time unknown" placeholder; a same-day copy may know the real time.
            same_day = [t for t in twins if t.date() == date.date() and t.time() != dt.time(12)]
            if same_day:
                date = same_day[0]
        if rule == "exif" and a and not is_video:
            # Trustworthy capture date, but Immich shows something else (e.g. a copy date
            # from an older import): put the capture date back. Not for videos: an edited
            # video's QuickTime creation_time is the edit date.
            cap = dt.datetime.fromisoformat(capture[:19])
            if abs(dt.datetime.fromisoformat(a["localDateTime"][:19]) - cap) > dt.timedelta(days=1):
                date, rule = cap, "Date/From-Exif"
        item = dict(path=path, size=size, mtime=mtime, sha=sha, capture=capture, video=is_video,
                    date=date, rule=rule, asset=a, album=album_name(rules, path), skip=None)
        if not a:
            min_b = lookup(path, rules["min_image_bytes_overrides"]) or rules["min_image_bytes"]
            if is_video and match(path, rules["no_videos"]):
                item["skip"] = "video excluded here"
            elif is_video and rules["max_video_mb"] and size > rules["max_video_mb"] * 1e6:
                item["skip"] = f"video > {rules['max_video_mb']} MB"
            elif not is_video and size > rules["max_file_mb"] * 1e6:
                item["skip"] = f"file > {rules['max_file_mb']} MB"
            elif not is_video and size < min_b:
                item["skip"] = f"image < {min_b // 1000} KB"
            elif rules["skip_probable_duplicates"]:
                raw = (date.date().isoformat() if date and rule == "Date/From-Export" else capture[:10])
                if raw and raw in by_name.get(norm_name(path.rsplit("/", 1)[1]), ()):
                    item["skip"] = "probable duplicate (same name + day)"
        plan.append(item)
    # One photo stored in several folders must get one answer, or runs would flip-flop
    # between them: every copy takes the best-ranked date among all copies.
    best = {}
    for i in plan:
        if i["asset"] and i["date"] is not None:
            k = i["asset"]["id"]
            if k not in best or RULE_RANK.index(i["rule"]) < RULE_RANK.index(best[k]["rule"]):
                best[k] = i
    for i in plan:
        b = i["asset"] and best.get(i["asset"]["id"])
        if b and b is not i:
            i["date"], i["rule"] = b["date"], b["rule"]
    return plan, assets


RULE_RANK = ["Date/From-Camera-Sidecar", "Date/From-Export", "Date/From-Exif", "Date/From-Filename", "Date/From-Folder",
             "Date/From-Modified", "Date/From-Duplicate", "Date/Unknown-Scan", "Date/Unknown-Modern"]


def needs_date(item, done):
    """done: {asset id: date this tool set}. A date the tool set stays the tool's to improve
    (better rules, a real time instead of noon) until someone edits it in Immich; after
    that it is left alone. Set (and tag) even when the date already matches, so every
    inferred date is findable."""
    a, new = item["asset"], item["date"]
    if new is None:
        return False
    if not a or a["id"] not in done:
        return True
    ours = done[a["id"]][:19]
    if a["localDateTime"][:19] != ours or new.isoformat(timespec="seconds") == ours:
        return False   # edited in Immich since (leave it), or nothing to change
    # Improve, don't churn (2026-11-12: one run re-dated 732 assets that were already dated,
    # 615 of them by a few hours, because a new copy of a photo in Files changed which
    # copy's file time was used, and 6 real dates fell back to the 1905 placeholder when
    # the Immich duplicate that had supplied them was gone).
    try:
        cur = dt.datetime.fromisoformat(ours)
    except ValueError:
        return True
    placeholder = cur.year in (1900, 1905)
    if item["rule"].startswith("Date/Unknown") and not placeholder:
        return False   # never trade a real date for "unknown"
    if not placeholder and cur.time() != dt.time(12) and abs(new - cur) < dt.timedelta(days=1):
        return False   # same day, and a real time of day already set: keep it
    return True


def write_report(plan, assets, done_ids, dry):
    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    top = lambda p: "/".join(p.split("/")[:2])
    up = [i for i in plan if not i["asset"] and not i["skip"]]
    sk = [i for i in plan if not i["asset"] and i["skip"]]
    dates = [i for i in plan if needs_date(i, done_ids) and not i["skip"]]
    gb = lambda L: f"{sum(i['size'] for i in L) / 1e9:.1f} GB"
    lines = [f"# immich-sync {'dry run' if dry else 'run'} {stamp}", "",
             f"- local candidates: {len(plan)}",
             f"- already in Immich: {sum(1 for i in plan if i['asset'])}",
             f"- to upload: {len(up)} ({gb(up)}) — images {sum(1 for i in up if not i['video'])}, videos {sum(1 for i in up if i['video'])}",
             f"- skipped: {len(sk)} ({gb(sk)})", "", "## Skipped by reason", ""]
    for k, v in collections.Counter(i["skip"] for i in sk).most_common():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Upload by folder", ""]
    for k, v in collections.Counter(top(i["path"]) for i in up).most_common():
        lines.append(f"- {v} {k}")
    lines += ["", "## Dates to set, by rule", ""]
    for rule, n in collections.Counter(i["rule"] for i in dates).most_common():
        changed = sum(1 for i in dates if i["rule"] == rule and i["asset"] and
                      abs((dt.datetime.fromisoformat(i["asset"]["localDateTime"][:19]) - i["date"]).total_seconds()) > 86400)
        lines.append(f"### {rule}: {n} (existing assets moving > 1 day: {changed})")
        for k, v in collections.Counter(top(i["path"]) for i in dates if i["rule"] == rule).most_common(8):
            lines.append(f"- {v} {k}")
        for i in [i for i in dates if i["rule"] == rule][:: max(1, n // 6)][:6]:
            cur = i["asset"]["localDateTime"][:10] if i["asset"] else "(new)"
            lines.append(f"  - `{i['path']}`: {cur} → {i['date']:%Y-%m-%d}")
        lines.append("")
    md = REPORTS / f"{stamp}{'-dry' if dry else ''}.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    with open(REPORTS / f"{stamp}{'-dry' if dry else ''}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "action", "rule", "current", "new", "album", "skip"])
        for i in plan:
            action = "upload" if not i["asset"] and not i["skip"] else ("skip" if i["skip"] else "in-immich")
            w.writerow([i["path"], action, i["rule"], i["asset"]["localDateTime"][:19] if i["asset"] else "",
                        i["date"].isoformat() if i["date"] else "", i["album"], i["skip"] or ""])
    log(f"report: {md}")
    print("\n".join(lines[:12]))


def upload(api, rules, item):
    p = Path(rules["root"]) / item["path"]
    st = p.stat()
    mtime = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()
    created = item["date"].isoformat() + "Z" if item["date"] else mtime
    name = p.name
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in (rules.get("upload_as") or {}):
        name = f"{name.rsplit('.', 1)[0]}.{rules['upload_as'][ext]}"
    # Streamed from disk: requests' own multipart builds the whole body in memory, which
    # for several multi-GB videos at once exhausted Windows socket buffers (WinError 10055).
    for attempt in range(5):
        try:
            with open(p, "rb") as fh:
                body = MultipartEncoder({
                    "deviceAssetId": f"{name}-{st.st_size}".replace(" ", ""), "deviceId": "immich-sync",
                    "fileCreatedAt": created, "fileModifiedAt": mtime, "isFavorite": "false",
                    "assetData": (name, fh, "application/octet-stream")})
                r = api.s.post(api.url + "/assets", data=body, timeout=3600,
                               headers={"Content-Type": body.content_type})
            if r.status_code < 500:
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code} {r.text[:300]}")
                return r.json()["id"]
            raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == 4:
                raise
            log(f"retry upload {item['path']}: {e}")
            time.sleep(5 * (attempt + 1))


def load_done(db):
    return dict(db.execute("SELECT asset_id, date FROM dated"))


def cmd_sync(rules, db, clients, dry, limit=None):
    """Returns the number of uploads that failed."""
    api = next(iter(clients.values()))
    plan, assets = build_plan(rules, db, clients)
    write_report(plan, assets, load_done(db), dry)
    if dry:
        return 0
    up = [i for i in plan if not i["asset"] and not i["skip"]][:limit]
    if limit:
        plan = [i for i in plan if i in up]
    log(f"sync: uploading {len(up)} files")
    n = failed = rejected = 0

    def work(i):
        try:
            return i, upload(api, rules, i)
        except RuntimeError as e:
            # A 4xx: Immich refuses this file (corrupt, unsupported). Retrying won't change
            # that and the file is safe in Files either way, so it is listed, not fatal -
            # otherwise one bad file would keep the job red for good.
            log(f"upload rejected by Immich {i['path']}: {e}")
            return i, False
        except Exception as e:
            log(f"upload failed {i['path']}: {e}")
            return i, None

    batch = rules["upload_batch"]
    with ThreadPoolExecutor(rules["upload_workers"]) as ex:
        for k in range(0, len(up), batch):
            if rules["max_queue_backlog"] is not None:
                api.wait_for_queues(rules["max_queue_backlog"])
            for i, aid in ex.map(work, up[k:k + batch]):
                if aid:
                    db.execute("UPDATE files SET asset_id=? WHERE path=?", (aid, i["path"]))
                    i["asset"] = {"id": aid}
                elif aid is None:
                    failed += 1
                else:
                    rejected += 1
                n += 1
            db.commit()
            log(f"sync: {n}/{len(up)}")
    db.commit()
    if rejected:
        log(f"sync: {rejected} files rejected by Immich (listed above; add them to rules.yaml "
            "exclude to stop retrying)")
    if failed:
        log(f"sync: FAILED - {failed} of {len(up)} uploads did not go through")
    if not rules.get("create_albums", True):
        return failed
    albumed = {r[0] for r in db.execute("SELECT asset_id FROM albumed")}
    want = collections.defaultdict(list)
    for i in plan:
        if i["album"] and i["asset"] and i["asset"]["id"] not in albumed:
            want[i["album"]].append(i["asset"]["id"])
    if want:
        existing = {a["albumName"]: a["id"] for a in api.call("GET", "/albums")}
        log(f"albums: adding {sum(map(len, want.values()))} assets to {len(want)} albums")
        for name, ids in want.items():
            ids = list(dict.fromkeys(ids))
            if name not in existing:
                existing[name] = api.call("POST", "/albums", json={"albumName": name})["id"]
            for k in range(0, len(ids), 1000):
                api.call("PUT", f"/albums/{existing[name]}/assets", json={"ids": ids[k:k + 1000]})
            db.executemany("INSERT OR REPLACE INTO albumed VALUES(?,?)", [(x, name) for x in ids])
            db.commit()
    return failed


def cmd_fix_dates(rules, db, clients, dry, limit=None):
    if not dry:
        next(iter(clients.values())).wait_for_metadata()
    plan, assets = build_plan(rules, db, clients)
    done = load_done(db)
    theirs = sum(1 for i in plan if i["rule"] == "partner")
    if theirs:
        log(f"fix-dates: skipping {theirs} files owned by an account with no key in rules.yaml")
    todo = [i for i in plan if i["asset"] and needs_date(i, done)]
    if limit:
        todo = [i for i in todo if i["path"] in limit]
    if dry:
        write_report(plan, assets, done, dry)
        return
    log(f"fix-dates: {len(todo)} assets")
    seen, total = set(), 0
    for owner, api in clients.items():  # each account can only edit its own assets
        mine = [i for i in todo if i["asset"].get("ownerId", owner) == owner and i["asset"]["id"] not in seen]
        if not mine:
            continue
        tag_names = sorted({i["rule"] for i in mine})
        tags = {t["value"]: t["id"] for t in api.call("PUT", "/tags", json={"tags": tag_names})}
        by_date, by_tag = collections.defaultdict(list), collections.defaultdict(list)
        for i in mine:
            aid = i["asset"]["id"]
            if aid in seen:  # same content in two places: first path wins
                continue
            seen.add(aid)
            by_date[i["date"].isoformat(timespec="seconds") + ".000Z"].append(i)
            by_tag[tags[i["rule"]]].append(aid)
        for when, items in by_date.items():
            ids = [i["asset"]["id"] for i in items]
            for k in range(0, len(ids), 1000):
                api.call("PUT", "/assets", json={"ids": ids[k:k + 1000], "dateTimeOriginal": when})
            # `old` keeps the date from before the tool first touched the asset, for undo.
            db.executemany("""INSERT INTO dated VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE
                              SET path=excluded.path, rule=excluded.rule, date=excluded.date, at=excluded.at""",
                           [(i["asset"]["id"], i["path"], i["rule"], when, NOW.isoformat(),
                             i["asset"].get("localDateTime")) for i in items])
            db.commit()
        for tid, ids in by_tag.items():
            for k in range(0, len(ids), 1000):
                api.call("PUT", "/tags/assets", json={"tagIds": [tid], "assetIds": ids[k:k + 1000]})
        log(f"fix-dates: {api.name}: set {sum(map(len, by_date.values()))} dates")
        total += sum(map(len, by_date.values()))
    log(f"fix-dates: set {total} dates")


# ---------------------------------------------------------------- Immich -> Files

WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)


def win_safe(name, fallback):
    """A file/folder name NTFS will take: G:\\Files is mirrored to NTFS and exFAT drives,
    and one bad name fails the whole mirror job."""
    name = WIN_BAD.sub("_", name).rstrip(" .")
    if not name or name.startswith("."):
        name = fallback + name
    if WIN_RESERVED.match(name):
        name = "_" + name
    return name


def file_sha(path):
    try:
        return sha1_b64(path)
    except OSError:
        return None


def cmd_backfill(rules, db, clients, dry):
    """Download every Immich original that has no byte-identical copy anywhere under the
    scanned folders, into backfill_dir/<owner>/<year>/<year-month>/. Immich is the only
    copy of these - photos the app uploaded and the phone later lost, imports whose source
    folder is gone, and full versions of files the phone backups only got half of - and
    Immich itself lives on the NAS pool, which nothing else backs up. Run after `scan`:
    the files table is what counts as "in Files". Returns the number of failures."""
    root = Path(rules["root"])
    api = next(iter(clients.values()))
    # A download cut off by Stop leaves a .part that every mirror would keep forever.
    for dirpath, _, names_ in os.walk(root / rules["backfill_dir"]):
        for n in names_:
            if n.endswith(".part"):
                try:
                    os.remove(os.path.join(dirpath, n))
                except OSError:
                    pass
    # Re-hash our own copies every run. The scan only re-hashes a file whose size or
    # mtime changed, so bit rot would hide behind the cache; this folder is small, and
    # it is the only copy of what is in it once the NAS is gone.
    rehashed = 0
    for rel, cached in db.execute("SELECT path, sha1 FROM files").fetchall():
        if in_backfill(rules, rel):
            now = file_sha(root / rel)
            if now != cached:
                log(f"backfill: {rel} changed on disk since it was saved")
                db.execute("UPDATE files SET sha1=? WHERE path=?", (now, rel))
            rehashed += 1
    db.commit()
    have = {r[0] for r in db.execute("SELECT sha1 FROM files WHERE sha1 IS NOT NULL")}
    # Every account's own view, merged: partner sharing shows Shelby's photos to George's
    # key today, but if sharing were ever turned off her Immich-only photos would drop out.
    assets = list({x["id"]: x for c in clients.values() for x in c.every_asset()}.values())
    # Folder per owner, by name: partners' names come from the user list, so the folder
    # doesn't depend on whether rules.yaml happens to hold that person's key.
    names = {u["id"]: u["name"] for u in (api.call("GET", "/users") or [])}
    names.update({oid: c.name for oid, c in clients.items()})
    todo, seen, motion, trashed = [], set(), 0, 0
    for a in assets:
        if a.get("isTrashed"):
            trashed += 1      # on its way out of Immich; not something to preserve
            continue
        if "/encoded-video/" in (a.get("originalPath") or ""):
            motion += 1       # clip Immich cut out of a motion photo; the still holds it
            continue
        if a["checksum"] in have or a["checksum"] in seen:
            continue
        seen.add(a["checksum"])
        todo.append(a)
    size = lambda a: (a.get("exifInfo") or {}).get("fileSizeInByte") or 0
    need = sum(size(a) for a in todo)
    log(f"backfill: re-verified {rehashed} earlier copies; {len(assets)} Immich assets, {len(todo)} "
        f"without a copy in Files ({need / 1e9:.1f} GB); skipped {motion} extracted motion clips, "
        f"{trashed} trashed")

    # Which asset each earlier backfill wrote, by path (scan keeps asset_id on rehash).
    ours = {p.lower(): aid for p, aid in db.execute(
        "SELECT path, asset_id FROM files WHERE asset_id IS NOT NULL") if in_backfill(rules, p)}

    def place(a, rel, dest):
        """'free' to write, 'same' if already there intact, or 'other' if the name holds
        anything else - never overwritten. That includes our own earlier copy of this asset
        whose bytes have since changed: rot and a deliberate edit (a photo rotated in
        Windows) look the same from here, so a fresh copy is saved beside it instead.
        Something else may also be the only copy of a photo since deleted from Immich."""
        if rel.lower() in taken:
            return "other"
        if not dest.exists():
            return "free"
        if file_sha(dest) == a["checksum"]:
            return "same"
        if ours.get(rel.lower()) == a["id"]:
            log(f"backfill: {rel} has changed since it was saved; keeping it and saving a "
                "fresh copy from Immich beside it")
        return "other"

    plan, taken, unplaced = [], set(), 0
    for a in todo:
        when = (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10]
        dated = [when[:4], when[:7]] if re.match(r"(18|19|20)\d\d-\d\d", when) else ["Unknown date"]
        owner = names.get(a["ownerId"]) or f"User {a['ownerId'][:8]}"
        folder = "/".join([rules["backfill_dir"].rstrip("/"), win_safe(owner, "owner")] + dated)
        fname = win_safe(a["originalFileName"] or "", a["id"])
        rel = f"{folder}/{fname}"
        dest = root / rel
        state = place(a, rel, dest)
        if state == "other":
            # Same name, different photo (IMG_0001.JPG from two phones). The asset id makes
            # the second name unique and stable, so a re-run lands on the same file.
            stem, dot, ext = fname.rpartition(".")
            fname = f"{stem or ext} (immich {a['id'][:8]}){dot}{ext if stem else ''}"
            rel = f"{folder}/{fname}"
            dest = root / rel
            state = place(a, rel, dest)
            if state == "other":
                log(f"backfill: FAILED - no free name for {a['id']} in {folder}")
                unplaced += 1
                continue
        taken.add(rel.lower())
        plan.append((a, rel, dest, state))

    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    csv_path = REPORTS / f"backfill-{stamp}{'-dry' if dry else ''}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "asset_id", "owner", "taken", "bytes", "visibility", "state"])
        for a, rel, _, state in plan:
            w.writerow([rel, a["id"], a["ownerId"], a.get("localDateTime", "")[:19], size(a),
                        a.get("visibility"), state])
    log(f"backfill: list in {csv_path}")
    if dry or not plan:
        return unplaced

    free = shutil.disk_usage(root).free
    floor = rules["backfill_min_free_gb"] * 1e9
    if free - need < floor:
        log(f"backfill: NOT RUN - needs {need / 1e9:.1f} GB and {root.anchor} has {free / 1e9:.1f} GB free "
            f"(floor {rules['backfill_min_free_gb']} GB, backfill_min_free_gb in rules.yaml)")
        return len(plan) + unplaced

    ok, failed = 0, unplaced
    for a, rel, dest, state in plan:
        if state == "same":    # already downloaded by an earlier, interrupted run
            got = True
        else:                  # free: the download lands via a .part, only once verified
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Download with the owner's own key where there is one; partner access is read-only anyway.
            got = clients.get(a["ownerId"], api).download(f"/assets/{a['id']}/original", dest, a["checksum"])
        if not got:
            failed += 1
            continue
        modified = a.get("fileModifiedAt") or a.get("fileCreatedAt")
        if modified:
            t = dt.datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
            os.utime(dest, (t, t))
        st = dest.stat()
        # Recorded as scanned so the next sync sees it as in Immich; capture_read=0 makes
        # the next scan read its EXIF date like any other file.
        db.execute("""INSERT INTO files(path,size,mtime,sha1,capture,capture_read,asset_id)
                      VALUES(?,?,?,?,'',0,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,
                      mtime=excluded.mtime, sha1=excluded.sha1, asset_id=excluded.asset_id""",
                   (rel, st.st_size, st.st_mtime, a["checksum"], a["id"]))
        ok += 1
        if ok % 100 == 0:
            db.commit()
            log(f"backfill: {ok}/{len(plan)}")
    db.commit()
    log(f"backfill: saved {ok} originals to {root / rules['backfill_dir']}, {failed} failed")
    return failed


DB_LATEST, DB_PREVIOUS = "immich-db-latest.sql.gz", "immich-db-previous.sql.gz"


def cmd_db_backup(rules, clients, dry):
    """Copy Immich's newest nightly database dump (albums, people, favourites, edits -
    everything that isn't the files themselves) into db_backup_dir. The names are fixed,
    latest + previous, so G: itself holds two dumps. The mirrors still keep history:
    savesheet's rclone jobs run with --suffix, so each dump they replace stays there
    under a dated name (~1.4 GB per run of this job). The dump's real name (it carries
    the Immich version) is in the .txt beside each one.
    Returns the number of failures."""
    admin = next((c for c in clients.values() if c.is_admin), None)
    if not admin:
        log("db-backup: FAILED - none of the rules.yaml accounts is an Immich admin")
        return 1
    backups = admin.call("GET", "/admin/database-backups")["backups"]
    if not backups:
        log("db-backup: FAILED - Immich lists no database backups (Administration > "
            "Settings > Backup Settings must be on)")
        return 1
    # Only finished nightly dumps, immich-db-backup-YYYYMMDDTHHMMSS-...sql.gz, which sort by
    # time. The list also holds failed or in-progress ones and dumps uploaded for a restore.
    backups = [b for b in backups if re.fullmatch(r"immich-db-backup-\d{8}T\d{6}-.+\.sql\.gz", b["filename"])]
    if not backups:
        log("db-backup: FAILED - Immich lists no finished nightly dump")
        return 1
    newest = max(backups, key=lambda b: b["filename"])
    out = Path(rules["root"]) / rules["db_backup_dir"]
    latest, note = out / DB_LATEST, out / (DB_LATEST + ".txt")
    stamp = re.search(r"(\d{8})T(\d{6})", newest["filename"])
    # Still saved (it's the best there is), but the job fails: Immich's own nightly dump
    # has stopped, and a green job would hide that.
    stale = int(bool(stamp) and dt.datetime.strptime("".join(stamp.groups()), "%Y%m%d%H%M%S")
                < NOW - dt.timedelta(days=3))
    if stale:
        log(f"db-backup: FAILED - Immich's newest dump {newest['filename']} is over 3 days old; "
            "its nightly backup job has stopped (Administration > Settings > Backup Settings)")
    current = note.read_text(encoding="utf-8").splitlines()[0] if note.exists() else ""
    if current == newest["filename"] and latest.exists() and latest.stat().st_size == newest["filesize"]:
        log(f"db-backup: {newest['filename']} already saved")
        return stale
    log(f"db-backup: {newest['filename']} ({newest['filesize'] / 1e6:.0f} MB) -> {latest}")
    if dry:
        return stale
    out.mkdir(parents=True, exist_ok=True)
    part = out / "immich-db-download.sql.gz"
    for leftover in (part, part.with_name(part.name + ".part")):   # from a run cut off by Stop
        leftover.unlink(missing_ok=True)
    if not admin.download(f"/admin/database-backups/{newest['filename']}", part):
        return 1
    try:
        if part.stat().st_size != newest["filesize"]:
            raise OSError(f"size {part.stat().st_size} != {newest['filesize']}")
        with gzip.open(part, "rb") as fh:      # reads it end to end: checks the gzip CRC
            while fh.read(1 << 24):
                pass
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        log(f"db-backup: FAILED - downloaded dump is damaged ({e}); kept the previous one")
        part.unlink(missing_ok=True)
        return 1
    if latest.exists():
        os.replace(latest, out / DB_PREVIOUS)
        if note.exists():
            os.replace(note, out / (DB_PREVIOUS + ".txt"))
    os.replace(part, latest)
    version = admin.call("GET", "/server/version") or {}
    note.write_text(
        f"{newest['filename']}\n\n"
        f"Immich database dump, downloaded {NOW:%Y-%m-%d %H:%M} from Immich "
        f"v{version.get('major')}.{version.get('minor')}.{version.get('patch')} by immich_sync.py.\n"
        "Restore it into a fresh Immich of the same version with Immich's own database\n"
        "backup restore (it accepts an uploaded dump; rename it back to the name on the\n"
        "first line first), or gunzip -c it into psql. It points at the originals by their\n"
        "path in Immich's upload folder, which the copies in G:\\Files do not keep - see\n"
        "'Rebuilding Immich' on the wiki's backups page before relying on it.\n",
        encoding="utf-8")
    log("db-backup: saved and verified")
    return stale


def cmd_report(api):
    assets = api.all_assets()
    ts = collections.Counter(a["localDateTime"][:19] for a in assets)
    REPORTS.mkdir(exist_ok=True)
    lines = [f"# Immich date clusters {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"{len(assets)} assets", "", "## Timestamps shared by 25+ assets", ""]
    for t, n in ts.most_common():
        if n < 25:
            break
        names = [a["originalFileName"] for a in assets if a["localDateTime"][:19] == t][:3]
        lines.append(f"- {t}: {n}  e.g. {', '.join(names)}")
    for hhmmss in ("12:00:00", "00:00:00"):
        days = collections.Counter(a["localDateTime"][:10] for a in assets if a["localDateTime"][11:19] == hhmmss)
        lines += ["", f"## Exactly {hhmmss}: {sum(days.values())} assets (busiest days)", ""]
        lines += [f"- {d}: {n}" for d, n in days.most_common(15)]
    out = REPORTS / f"clusters-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"report: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["scan", "sync", "fix-dates", "backfill", "db-backup", "report", "all"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="sync: upload only the first N (testing)")
    ap.add_argument("--only", nargs="*", help="fix-dates: only these relative paths (testing)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rules, db = load_rules(), open_state()
    clients = {}
    for auth in rules["accounts"]:
        c = Immich(auth)
        me = c.call("GET", "/users/me")
        c.name = me["name"]
        c.is_admin = bool(me.get("isAdmin"))
        clients[me["id"]] = c
    api = next(iter(clients.values()))
    failures = 0
    # backfill decides what Files lacks from the scan, so it always scans first. The two
    # steps that protect data (backfill, db-backup) run before sync and fix-dates: an API
    # error there ends the run (Immich.call exits on a 4xx), and it must not cost the copies.
    if a.command in ("scan", "backfill", "all"):
        scan(rules, db)
    if a.command in ("backfill", "all"):
        failures += cmd_backfill(rules, db, clients, a.dry_run)
    if a.command in ("db-backup", "all"):
        failures += cmd_db_backup(rules, clients, a.dry_run)
    if a.command in ("sync", "all"):
        failures += cmd_sync(rules, db, clients, a.dry_run, a.limit)
    if a.command == "fix-dates" or (a.command == "all" and not a.dry_run):
        cmd_fix_dates(rules, db, clients, a.dry_run, set(a.only) if a.only else None)
    if a.command in ("report", "all"):
        cmd_report(api)
    if failures:
        # Fail the savesheet job: a photo or dump that didn't make it into Files is not backed up.
        raise SystemExit(f"{failures} upload/backfill/db-backup failure(s); see the log above")


if __name__ == "__main__":
    main()
