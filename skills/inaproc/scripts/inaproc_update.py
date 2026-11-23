"""Keep an installed inaproc skill up to date from GitHub, standard library only.

At most once a day (after a command has finished) the installed copy reads `skills/inaproc/version.json` on
the repository's main branch. When it is newer, the repository tarball is downloaded, extracted safely,
checked (same version, required files present) and swapped in, with the old copy kept in
~/.inaproc/backups/. Never runs inside a git checkout, in a read-only folder, or with INAPROC_AUTO_UPDATE=0.
"""
import io
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import time
import urllib.request

REPO = "bahrulbangsawan/inaproc"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/skills/inaproc/version.json"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/main"
CHECK_EVERY = 24 * 3600
REQUIRED = ("version.json", "SKILL.md", "scripts/inaproc.py")
KEEP_LOCAL = ("scripts/queries.discovered.json",)  # files a user generated in the installed copy


def _home():
    p = pathlib.Path(os.environ.get("INAPROC_HOME") or (pathlib.Path.home() / ".inaproc")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_skill_dir():
    return pathlib.Path(__file__).resolve().parent.parent


def _fetch(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "inaproc-skill-updater"})
    with urllib.request.urlopen(req, timeout=10 if "codeload" in url else 3) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"{url}: larger than {limit} bytes")
    return data


def _vt(v):
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def newer(a, b):
    return _vt(a) > _vt(b)


def can_self_update(skill_dir):
    if os.environ.get("INAPROC_AUTO_UPDATE", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    d = pathlib.Path(skill_dir).resolve()
    for p in [d] + list(d.parents):
        if (p / ".git").exists():
            return False  # a developer checkout: never overwrite it
    return os.access(d, os.W_OK) and os.access(d.parent, os.W_OK)


def latest_version(fetch=_fetch):
    return json.loads(fetch(VERSION_URL, 4096).decode("utf-8"))["version"]


def _safe_extract(data, dest):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        dest_real = os.path.realpath(dest)
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if m.issym() or m.islnk() or m.isdev() or not (target == dest_real or
                                                           target.startswith(dest_real + os.sep)):
                raise OSError(f"unsafe path in archive: {m.name}")
        for m in t.getmembers():
            if m.isdir():
                os.makedirs(os.path.join(dest, m.name), exist_ok=True)
            elif m.isfile():
                path = os.path.join(dest, m.name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with t.extractfile(m) as src, open(path, "wb") as out:
                    shutil.copyfileobj(src, out)


def install(version, skill_dir, current, fetch=_fetch):
    """Download main, check it is `version`, back up `skill_dir` and swap the new skill folder in."""
    skill_dir = pathlib.Path(skill_dir)
    with tempfile.TemporaryDirectory() as tmp:
        _safe_extract(fetch(TARBALL_URL, 30 * 1024 * 1024), tmp)
        found = [p.parent for p in pathlib.Path(tmp).glob("*/skills/inaproc/version.json")]
        if len(found) != 1:
            raise OSError("the download does not contain skills/inaproc")
        new = found[0]
        for rel in REQUIRED:
            if not (new / rel).is_file():
                raise OSError(f"the download is missing {rel}")
        got = json.loads((new / "version.json").read_text(encoding="utf-8"))["version"]
        if got != version:
            raise OSError(f"the download is version {got}, expected {version}")
        for rel in KEEP_LOCAL:
            if (skill_dir / rel).exists() and not (new / rel).exists():
                shutil.copy2(skill_dir / rel, new / rel)
        shutil.rmtree(new / "scripts" / "__pycache__", ignore_errors=True)
        backups = _home() / "backups"
        backups.mkdir(exist_ok=True)
        shutil.copytree(skill_dir, backups / f"inaproc-{current}-{time.strftime('%Y%m%d-%H%M%S')}",
                        ignore=shutil.ignore_patterns("__pycache__"))
        staging = skill_dir.with_name(".inaproc-update")
        old = skill_dir.with_name(".inaproc-previous")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        shutil.copytree(new, staging)
        os.rename(skill_dir, old)
        try:
            os.rename(staging, skill_dir)
        except OSError:
            os.rename(old, skill_dir)  # put the working copy back
            raise
        shutil.rmtree(old, ignore_errors=True)
        try:
            os.chmod(skill_dir / "scripts" / "inaproc.py", 0o755)
        except OSError:
            pass


def _state_path():
    return _home() / "update.json"


def maybe_update(current, skill_dir=None, fetch=_fetch, force=False):
    """Returns a one-line message when an update was installed, else None. Never raises."""
    try:
        skill_dir = pathlib.Path(skill_dir or default_skill_dir())
        if not can_self_update(skill_dir):
            return None
        state = {}
        try:
            state = json.loads(_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not force and time.time() - float(state.get("lastCheck", 0)) < CHECK_EVERY:
            return None
        state["lastCheck"] = time.time()
        _state_path().write_text(json.dumps(state), encoding="utf-8")
        latest = latest_version(fetch)
        state["latest"] = latest
        _state_path().write_text(json.dumps(state), encoding="utf-8")
        if not newer(latest, current):
            return None
        install(latest, skill_dir, current, fetch)
        return f"inaproc updated {current} → {latest} (takes effect from the next command)"
    except Exception:
        return None


def cmd_update(a):
    from inaproc import VERSION
    latest = latest_version()
    if not newer(latest, VERSION):
        print(f"inaproc {VERSION} is the latest version.")
        return
    if a.check:
        print(f"inaproc {latest} is available (installed: {VERSION}). Run `update` to install it.")
        return
    skill_dir = default_skill_dir()
    if not can_self_update(skill_dir):
        print(f"inaproc {latest} is available, but this copy ({skill_dir}) is a git checkout, read-only, or has "
              "INAPROC_AUTO_UPDATE=0. Update it with `git pull` or ./install.sh instead.")
        return
    install(latest, skill_dir, VERSION)
    print(f"inaproc updated {VERSION} → {latest}. The old copy is in {_home() / 'backups'}.")
