#!/usr/bin/env python3
"""The versions of repository-linked projects: an entry with `repo = "owner/name"` lists its
GitHub releases instead of `[[versions]]`. sync.yml runs this; publish.yml reads what it keeps.

  plan <checkout> <branch>             what is new: releases not read yet, a page due (JSON on stdout)
  apply <checkout> <branch> <plan> <out>
                                       reads them with `cargo horizon-mod registry release|page` and
                                       writes the files that change to <out>
  validate <checkout> <dir> [--drop]   the shape of <dir>/<id>.json, before it is trusted

<branch> is a copy of the `versions` branch: `versions/<id>.json` (what `registry build` reads:
the page and every version that passed its checks) and `state/<id>.json` (the newest tag whose
`horizon-registry.toml` was read, and the releases that failed, so they are not read every hour).

Everything read from a project's repository is untrusted data: nothing downloaded is run, and
the tool only reads packages. A release that fails is left out and reported; the rest go on.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# The index's own limit on a file.
MAX_FILE = 64 << 20
MAX_SIGNATURE = 64 << 10
MAX_PAGE = 64 << 10
# The tool keeps 6000 characters of the notes.
MAX_NOTES = 64 << 10
# Releases read per project and run, newest first; older ones wait for the next run.
PER_RUN = 10
EXTENSIONS = {
    "mod": "hmod",
    "modpack": "hpack",
    "resourcepack": "zip",
    "shader": "hshader",
    "shaderpack": "zip",
    "datapack": "zip",
}
# Every version of a native mod is reviewed: its code runs with the player's permissions.
REFUSED = {"native-mod": "a native mod's versions are each reviewed: list them in [[versions]]"}
ID = re.compile(r"[a-z0-9_]{1,64}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}")
TAG = re.compile(r"[A-Za-z0-9._+-]{1,128}")
PAGE_KEYS = {"name", "summary", "description", "authors", "license"}
TOOL = ["cargo", "horizon-mod", "registry"]


def flat(text):
    """One line: a log line that starts with `::` is a command to the runner, and the text here
    comes from repositories."""
    return " | ".join(part.strip() for part in str(text).splitlines() if part.strip())


def download_prefix(repo):
    return f"https://github.com/{repo}/releases/download/"


def valid_repo(repo):
    return (
        isinstance(repo, str)
        and REPO.fullmatch(repo) is not None
        and all(part not in (".", "..") for part in repo.split("/"))
    )


def entries(checkout):
    """The entries with `repo`: {id: {kind, repo, key}}. Others are the registry's own."""
    out = {}
    for path in sorted(pathlib.Path(checkout, "mods").glob("*.toml")):
        try:
            entry = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            print(f"::warning::{path.name}: {e}", file=sys.stderr)
            continue
        if "repo" not in entry or entry.get("id") != path.stem or not ID.fullmatch(path.stem):
            continue
        out[path.stem] = {
            "kind": entry.get("kind", ""),
            "repo": entry["repo"],
            "key": bool(entry.get("key")),
            # A failed release is read again once its entry changes.
            "entry": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return out


def read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def cached_tags(cache, repo):
    prefix = download_prefix(repo)
    tags = set()
    for v in cache.get("versions", []):
        url = v.get("file", {}).get("url", "")
        if url.startswith(prefix):
            tags.add(url[len(prefix):].split("/", 1)[0])
    return tags


def gh_api(path, raw=False):
    args = ["gh", "api", path]
    if raw:
        args[2:2] = ["-H", "Accept: application/vnd.github.raw+json"]
    return subprocess.run(args, capture_output=True, timeout=120, check=False)


def fingerprint(release, e):
    """What makes a failed release worth reading again: its files, or its entry, changed."""
    return {"assets": sorted(a["id"] for a in release["assets"]), "entry": e["entry"]}


def plan_project(id, e, branch, releases):
    """What is new for one project, or None."""
    cache = read_json(pathlib.Path(branch, "versions", f"{id}.json"), {})
    state = read_json(pathlib.Path(branch, "state", f"{id}.json"), {})
    seen = cached_tags(cache, e["repo"])
    failed = state.get("failed", {})
    listed = sorted(
        (r for r in releases if not r.get("draft") and not r.get("prerelease") and r.get("published_at")),
        key=lambda r: r["published_at"],
        reverse=True,
    )
    new = []
    for r in listed:
        tag = r.get("tag_name", "")
        release = {
            "tag": tag,
            "published": r["published_at"][:10],
            "notes": (r.get("body") or "")[:MAX_NOTES],
            "assets": [
                {"id": a["id"], "name": a["name"], "size": a["size"]}
                for a in r.get("assets", [])
                if a.get("state") == "uploaded"
            ],
        }
        if tag in seen:
            continue
        # A failed release is read again once its files change.
        if tag in failed and failed[tag].get("seen") == fingerprint(release, e):
            continue
        new.append(release)
    newest = listed[0]["tag_name"] if listed else None
    page_due = newest is not None and newest != state.get("page_tag")
    if not new and not page_due:
        return None
    return {"id": id, **e, "newest": newest, "page_due": page_due, "releases": new[:PER_RUN]}


def plan(checkout, branch):
    out, report = [], []
    for id, e in entries(checkout).items():
        if e["kind"] in REFUSED:
            report.append(f"{id}: {REFUSED[e['kind']]}")
            continue
        if e["kind"] not in EXTENSIONS:
            report.append(f"{id}: kind {e['kind']!r} has no release files this sync reads")
            continue
        if not valid_repo(e["repo"]):
            report.append(f"{id}: repo {e['repo']!r} is not owner/name")
            continue
        result = gh_api(f"repos/{e['repo']}/releases?per_page=100")
        try:
            if result.returncode != 0:
                raise ValueError(result.stderr.decode(errors="replace").strip()[:300])
            project = plan_project(id, e, branch, json.loads(result.stdout))
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            report.append(f"{id}: the releases of {e['repo']} can't be read: {err}")
            continue
        if project:
            out.append(project)
    return {"projects": out, "report": [flat(r) for r in report]}


class Transient(Exception):
    """Worth trying again next run."""


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "redirect away from https", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(HttpsOnly)


def download(url, path, limit):
    """`url` into `path`, refused past `limit` bytes. No token goes with it: the files are public."""
    request = urllib.request.Request(url, headers={"User-Agent": "horizon-mods-sync"})
    total = 0
    try:
        with OPENER.open(request, timeout=120) as response, open(path, "wb") as out:
            while chunk := response.read(1 << 16):
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"over {limit >> 10} KiB")
                out.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code >= 500 or e.code == 429:
            raise Transient(f"{url}: HTTP {e.code}") from e
        raise ValueError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise Transient(f"{url}: {e}") from e


def asset_url(repo, tag, name):
    q = lambda s: urllib.parse.quote(s, safe="")
    return f"{download_prefix(repo)}{q(tag)}/{q(name)}"


def tool(*args):
    """The tool's output, or ValueError with its message."""
    result = subprocess.run([*TOOL, *map(str, args)], capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout).strip()[-1500:])
    return result.stdout


def read_release(p, release, trial, work):
    """The index's version of one release, or ValueError."""
    tag = release["tag"]
    if not TAG.fullmatch(tag):
        raise ValueError(f"tag {tag!r}: use letters, digits, '.', '-', '_' and '+'")
    ext = EXTENSIONS[p["kind"]]
    files = [a for a in release["assets"] if a["name"].endswith(f".{ext}")]
    if len(files) != 1:
        raise ValueError(f"{len(files)} .{ext} files attached: attach exactly one")
    asset = files[0]
    if asset["size"] > MAX_FILE:
        raise ValueError(f"{asset['name']} is over {MAX_FILE >> 20} MiB")
    package = work / f"package.{ext}"
    download(asset_url(p["repo"], tag, asset["name"]), package, MAX_FILE)
    notes = work / "notes.md"
    notes.write_text(release["notes"], encoding="utf-8")
    args = [trial, p["id"], package, "--tag", tag, "--published", release["published"],
            "--notes", notes, "--name", asset["name"]]
    if p["key"]:
        sigs = [a for a in release["assets"] if a["name"] == f"{asset['name']}.minisig"]
        if not sigs:
            raise ValueError(f"the project has a key: attach {asset['name']}.minisig")
        signature = work / "package.minisig"
        download(asset_url(p["repo"], tag, sigs[0]["name"]), signature, MAX_SIGNATURE)
        args += ["--signature", signature]
    version = json.loads(tool("release", *args))
    if not version.get("file", {}).get("url", "").startswith(download_prefix(p["repo"])):
        raise ValueError("the tool listed a file outside the project's repository")
    return version


def read_page(p, trial, work):
    """The page at the newest release's tag (none: the entry's own fields), or ValueError."""
    path = f"repos/{p['repo']}/contents/horizon-registry.toml?ref={urllib.parse.quote(p['newest'], safe='')}"
    result = gh_api(path, raw=True)
    if result.returncode != 0:
        if b"HTTP 404" not in result.stderr:
            raise Transient(f"horizon-registry.toml can't be read: {result.stderr.decode(errors='replace').strip()[:300]}")
        text = b""
    else:
        text = result.stdout
    if len(text) > MAX_PAGE:
        raise ValueError(f"horizon-registry.toml is over {MAX_PAGE >> 10} KiB")
    page = work / "horizon-registry.toml"
    page.write_bytes(text)
    return json.loads(tool("page", trial, p["id"], page))


def trial_checkout(checkout, id, root):
    """A registry of this one project, for `registry build`: what publish would make of it."""
    checkout = pathlib.Path(checkout)
    (root / "mods").mkdir(parents=True)
    (root / "versions").mkdir()
    (root / "files").mkdir()
    shutil.copy(checkout / "mods" / f"{id}.toml", root / "mods")
    for name in ("revoked.toml", f"icons/{id}.png"):
        if (checkout / name).is_file():
            (root / name).parent.mkdir(exist_ok=True)
            shutil.copy(checkout / name, root / name)
    if (checkout / "screenshots" / id).is_dir():
        shutil.copytree(checkout / "screenshots" / id, root / "screenshots" / id)


def builds(trial, id, cache):
    """Whether publish's `registry build` takes the project with `cache`; ValueError if not."""
    (trial / "versions" / f"{id}.json").write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")
    tool("build", trial, "--files", trial / "files", "--out", trial / "index.json",
         "--generated", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def apply_project(checkout, branch, p, out, report):
    id = p["id"]
    cache = read_json(pathlib.Path(branch, "versions", f"{id}.json"), {"page": {}, "versions": []})
    cache.setdefault("page", {})
    cache.setdefault("versions", [])
    state = read_json(pathlib.Path(branch, "state", f"{id}.json"), {})
    state.setdefault("failed", {})
    cache_changed = state_changed = False
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        trial = tmp / "registry"
        trial_checkout(checkout, id, trial)
        if p["page_due"]:
            work = tmp / "page"
            work.mkdir()
            try:
                page = read_page(p, trial, work)
                candidate = {**cache, "page": page}
                builds(trial, id, candidate)
                if page != cache["page"]:
                    cache, cache_changed = candidate, True
                    report.append(f"{id}: page read at {p['newest']}")
                state["page_tag"], state_changed = p["newest"], True
            except Transient as e:
                report.append(f"{id}: page at {p['newest']} not read, next run tries again: {e}")
            except (ValueError, OSError, json.JSONDecodeError) as e:
                report.append(f"{id}: page at {p['newest']} left out: {e}")
                state["page_tag"], state_changed = p["newest"], True
        for n, release in enumerate(p["releases"]):
            work = tmp / f"release{n}"
            work.mkdir()
            tag = release["tag"]
            try:
                version = read_release(p, release, trial, work)
                if any(v.get("version") == version.get("version") for v in cache["versions"]):
                    raise ValueError(f"version {version.get('version')} is listed already")
                versions = sorted([*cache["versions"], version], key=lambda v: v.get("published", ""), reverse=True)
                candidate = {**cache, "versions": versions}
                builds(trial, id, candidate)
                cache, cache_changed = candidate, True
                if state["failed"].pop(tag, None) is not None:
                    state_changed = True
                report.append(f"{id} {version.get('version')}: listed from release {tag}")
            except Transient as e:
                report.append(f"{id}: release {tag} not read, next run tries again: {e}")
            except (ValueError, OSError) as e:
                state["failed"][tag] = {"seen": fingerprint(release, p), "reason": str(e)[:1500]}
                state_changed = True
                report.append(f"{id}: release {tag} left out: {e}")
                print(f"::warning::{id}: release {tag} left out", file=sys.stderr)
    for changed, folder, value in ((cache_changed, "versions", cache), (state_changed, "state", state)):
        if changed:
            (out / folder).mkdir(parents=True, exist_ok=True)
            (out / folder / f"{id}.json").write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")


def apply(checkout, branch, plan_file, out):
    plan = json.loads(pathlib.Path(plan_file).read_text(encoding="utf-8"))
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report = list(plan["report"])
    for p in plan["projects"]:
        try:
            apply_project(checkout, branch, p, out, report)
        except Exception as e:  # one project's trouble never stops the others
            report.append(f"{p['id']}: not synced: {e}")
            print(f"::error::{p['id']}: not synced", file=sys.stderr)
    return [flat(r) for r in report]


def problems(checkout, dir):
    """{file: problem} of a `versions/` folder: only `<id>.json` of an entry with `repo` and a kind
    this sync reads, in the shape `registry build` reads, every file an asset of the entry's own
    repository."""
    linked = entries(checkout)
    found = {}
    for path in sorted(pathlib.Path(dir).iterdir()):
        id = path.name.removesuffix(".json")
        e = linked.get(id)
        try:
            if not path.is_file() or not path.name.endswith(".json"):
                raise ValueError("only <id>.json files go here")
            if e is None or e["kind"] not in EXTENSIONS or not valid_repo(e["repo"]):
                raise ValueError("no entry with a repo for it")
            cache = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict) or not set(cache) <= {"page", "versions"}:
                raise ValueError("only page and versions")
            page, versions = cache.get("page", {}), cache.get("versions", [])
            if not isinstance(page, dict) or not set(page) <= PAGE_KEYS:
                raise ValueError(f"the page holds only {', '.join(sorted(PAGE_KEYS))}")
            if not isinstance(versions, list):
                raise ValueError("versions is a list")
            prefix = download_prefix(e["repo"])
            for v in versions:
                url = v.get("file", {}).get("url") if isinstance(v, dict) and isinstance(v.get("file"), dict) else None
                if not isinstance(url, str) or not url.startswith(prefix):
                    raise ValueError(f"a file outside {prefix}")
        except (ValueError, OSError) as err:
            found[path.name] = str(err)
    return found


def main(argv):
    command, *args = argv or [""]
    if command == "plan" and len(args) == 2:
        json.dump(plan(*args), sys.stdout, indent=1)
    elif command == "apply" and len(args) == 4:
        report = apply(*args)
        print("\n".join(report))
        changed = any(pathlib.Path(args[3]).rglob("*.json"))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"changed={'true' if changed else 'false'}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("## Releases read\n```\n" + ("\n".join(report) or "nothing new") + "\n```\n")
    elif command == "validate" and len(args) in (2, 3) and args[2:] in ([], ["--drop"]):
        found = problems(args[0], args[1])
        for name, problem in found.items():
            print(f"::error::versions/{name}: {problem}")
            if args[2:]:
                pathlib.Path(args[1], name).unlink()
        return 1 if found and not args[2:] else 0
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
