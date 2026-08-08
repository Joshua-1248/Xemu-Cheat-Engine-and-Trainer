"""Fetch and cache the shared cheat/patch database from GitHub.

The database lives in the project's own repository, in the same cheats/ and
patches/ folders and the same file format the trainer already reads -- so
nothing here has to know anything about the format. It downloads, unpacks,
and hands the paths back; cheatfiles.py does the parsing.

Why a tarball rather than the contents API: there are ~736 files in each
folder. Walking them through api.github.com is 1472 requests and runs into
the unauthenticated rate limit almost immediately (60/hour per IP). The
codeload tarball is one request, needs no token, and is what git itself
would pull.

The cache is deliberately kept apart from the user's own cheats/ and
patches/ folders. Config.save() rewrites every file it owns on every
autosave, so if database entries were mixed into those trees they would be
written into the user's files and become indistinguishable from their own
work the first time anything was toggled.
"""
from .prelude import *  # noqa: F401,F403
import os, sys, io, json, time, shutil, tarfile, tempfile, urllib.request, urllib.error

from . import cheatfiles

REPO = "Joshua-1248/Xemu-Cheat-Engine-and-Trainer"
BRANCH = "main"
TARBALL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
DB_DIRNAME = "db"
META_NAME = "db_meta.json"
TIMEOUT = 60


def db_dir(base_dir):
    return os.path.join(base_dir, DB_DIRNAME)


def db_path_for_stem(base_dir, kind, stem):
    folder = cheatfiles.CHEATS_DIR if kind == "cheats" else cheatfiles.PATCHES_DIR
    return os.path.join(db_dir(base_dir), folder, stem + ".txt")


def meta_path(base_dir):
    return os.path.join(db_dir(base_dir), META_NAME)


def read_meta(base_dir):
    """When the cache was last refreshed, and how much is in it."""
    try:
        with open(meta_path(base_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return {}


def status_line(base_dir):
    meta = read_meta(base_dir)
    if not meta:
        return "Database: not downloaded"
    when = meta.get("fetched", 0)
    stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if when else "?"
    return (f"Database: {meta.get('cheats', 0)} cheats, "
            f"{meta.get('patches', 0)} patches ({stamp})")


def download(base_dir, progress=None):
    """
    Refresh the local cache. Returns the new meta dict.

    Runs off the GUI thread -- `progress` is called with short status
    strings and must be marshalled back to Tk by the caller.

    Everything lands in a temp directory and is swapped in at the end, so an
    interrupted download cannot leave a half-populated database behind that
    would silently look like the real thing.
    """
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:                               # noqa: BLE001
                pass

    say("Contacting GitHub\u2026")
    req = urllib.request.Request(
        TARBALL, headers={"User-Agent": "xemu-cheat-trainer"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub returned HTTP {exc.code}") from exc
    except Exception as exc:                                # noqa: BLE001
        raise RuntimeError(f"could not reach GitHub ({exc})") from exc

    say(f"Unpacking {len(blob) // 1024} KB\u2026")
    counts = {"cheats": 0, "patches": 0}
    tmp = tempfile.mkdtemp(prefix="xemudb-", dir=base_dir)
    try:
        for kind in ("cheats", "patches"):
            folder = cheatfiles.CHEATS_DIR if kind == "cheats" \
                else cheatfiles.PATCHES_DIR
            os.makedirs(os.path.join(tmp, folder), exist_ok=True)

        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                parts = member.name.split("/")
                # <repo>-<branch>/<folder>/<file>.txt and nothing deeper.
                if len(parts) != 3:
                    continue
                folder, fn = parts[1], parts[2]
                if folder not in (cheatfiles.CHEATS_DIR, cheatfiles.PATCHES_DIR):
                    continue
                if not fn.lower().endswith(".txt"):
                    continue
                # Never trust a path out of an archive. Anything with a
                # separator or a parent reference in the filename is skipped
                # rather than sanitised -- real entries never have one.
                if os.path.basename(fn) != fn or fn in (".", ".."):
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = os.path.join(tmp, folder, fn)
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                kind = "cheats" if folder == cheatfiles.CHEATS_DIR else "patches"
                counts[kind] += 1

        if not counts["cheats"] and not counts["patches"]:
            raise RuntimeError("archive contained no cheat or patch files")

        meta = {"fetched": int(time.time()), "repo": REPO, "branch": BRANCH,
                "cheats": counts["cheats"], "patches": counts["patches"]}
        with open(os.path.join(tmp, META_NAME), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)

        say("Installing\u2026")
        target = db_dir(base_dir)
        old = target + ".old"
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(target):
            os.replace(target, old)
        os.replace(tmp, target)
        tmp = None
        shutil.rmtree(old, ignore_errors=True)
        try:
            xemu_privs.reclaim_tree(target)
        except Exception:                                   # noqa: BLE001
            pass
        say(status_line(base_dir))
        return meta
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def load_trees(base_dir):
    """
    Parse the cached database.

    Returns {stem: {'cheats': tree, 'patches': tree, 'name': str,
    'titleid': str, 'serial': str}}. Missing cache gives {} rather than
    raising, so the checkbox can be on before anything has been downloaded.
    """
    root = db_dir(base_dir)
    out = {}
    for kind in ("cheats", "patches"):
        folder = cheatfiles.CHEATS_DIR if kind == "cheats" \
            else cheatfiles.PATCHES_DIR
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith(".txt"):
                continue
            stem = fn[:-4]
            try:
                tree, meta = cheatfiles.read_cheat_file(os.path.join(d, fn))
            except Exception:                               # noqa: BLE001
                continue
            rec = out.setdefault(stem, {"cheats": [], "patches": [],
                                        "name": "", "titleid": "", "serial": ""})
            rec[kind] = tree
            rec["name"] = rec["name"] or (meta.get("game") or "").strip()
            rec["titleid"] = rec["titleid"] or (meta.get("titleid") or "").strip()
            rec["serial"] = rec["serial"] or (meta.get("serial") or "").strip()
    for stem, rec in out.items():
        fn_serial, fn_titleid = cheatfiles.split_stem(stem)
        rec["titleid"] = rec["titleid"] or fn_titleid
        rec["serial"] = rec["serial"] or fn_serial
        rec["name"] = rec["name"] or stem
    return out


def mark_db(nodes):
    """
    Tag a parsed tree as database-owned, in place.

    The flag is what keeps these entries out of the user's files: the save
    routine never sees them, and the editing commands refuse to touch a node
    carrying it. Enabled state is still allowed to change -- turning a
    database cheat on is the entire point of having them.
    """
    for node in nodes:
        node['_db'] = True
        kids = node.get('children')
        if isinstance(kids, list):
            mark_db(kids)
    return nodes
