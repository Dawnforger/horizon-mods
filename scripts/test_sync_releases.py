import json
import pathlib
import tempfile
import unittest

import sync_releases as sync

ENTRY = '''id = "{id}"
kind = "{kind}"
repo = "{repo}"
owners = ["author"]
'''


def release(tag, published, assets=(("pack.zip", 1),), **extra):
    return {
        "tag_name": tag,
        "published_at": f"{published}T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "body": "notes",
        "assets": [{"id": i, "name": n, "size": 10, "state": "uploaded"} for n, i in assets],
        **extra,
    }


class SyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        (self.root / "mods").mkdir()
        (self.root / "branch" / "versions").mkdir(parents=True)
        (self.root / "branch" / "state").mkdir()

    def entry(self, id="pack", kind="resourcepack", repo="author/pack"):
        (self.root / "mods" / f"{id}.toml").write_text(ENTRY.format(id=id, kind=kind, repo=repo))
        return sync.entries(self.root)[id]

    def cache(self, id, tags, repo="author/pack"):
        versions = [{"version": t, "file": {"name": "pack.zip", "url": f"https://github.com/{repo}/releases/download/{t}/pack.zip"}} for t in tags]
        (self.root / "branch" / "versions" / f"{id}.json").write_text(json.dumps({"versions": versions}))

    def test_only_entries_with_a_repo(self):
        (self.root / "mods" / "own.toml").write_text('id = "own"\nkind = "mod"\nowners = ["a"]\n')
        self.entry()
        self.assertEqual(list(sync.entries(self.root)), ["pack"])

    def test_new_releases_newest_first_without_drafts_prereleases_or_cached(self):
        e = self.entry()
        self.cache("pack", ["v1"])
        releases = [
            release("v1", "2026-11-01"),
            release("v3", "2026-11-03"),
            release("v2", "2026-11-02"),
            release("v4", "2026-11-04", draft=True),
            release("v5", "2026-11-05", prerelease=True),
        ]
        p = sync.plan_project("pack", e, self.root / "branch", releases)
        self.assertEqual([r["tag"] for r in p["releases"]], ["v3", "v2"])
        self.assertEqual(p["newest"], "v3")
        self.assertTrue(p["page_due"])

    def test_a_failed_release_waits_for_new_files_or_a_new_entry(self):
        e = self.entry()
        r = release("v1", "2026-11-01", assets=(("pack.zip", 7),))
        failed = {"v1": {"seen": {"assets": [7], "entry": e["entry"]}}}
        state = self.root / "branch" / "state" / "pack.json"
        state.write_text(json.dumps({"page_tag": "v1", "failed": failed}))
        self.assertIsNone(sync.plan_project("pack", e, self.root / "branch", [r]))
        r["assets"][0]["id"] = 8
        self.assertEqual(len(sync.plan_project("pack", e, self.root / "branch", [r])["releases"]), 1)
        r["assets"][0]["id"] = 7
        e = self.entry(repo="author/pack2")
        self.assertEqual(len(sync.plan_project("pack", e, self.root / "branch", [r])["releases"]), 1)

    def test_nothing_new(self):
        e = self.entry()
        self.cache("pack", ["v1"])
        (self.root / "branch" / "state" / "pack.json").write_text('{"page_tag": "v1"}')
        self.assertIsNone(sync.plan_project("pack", e, self.root / "branch", [release("v1", "2026-11-01")]))

    def test_validate(self):
        self.entry()
        self.entry(id="native", kind="native-mod", repo="author/native")
        versions = self.root / "branch" / "versions"
        self.cache("pack", ["v1"])
        self.cache("native", ["v1"], repo="author/native")
        (versions / "other.json").write_text("{}")
        (versions / "pack2.json").write_text("{}")
        self.assertEqual(sorted(sync.problems(self.root, versions)), ["native.json", "other.json", "pack2.json"])
        self.cache("pack", ["v1"], repo="someone/else")
        self.assertIn("pack.json", sync.problems(self.root, versions))
        (versions / "pack.json").write_text('{"page": {"key": "x"}}')
        self.assertIn("pack.json", sync.problems(self.root, versions))

    def test_a_later_release_of_the_same_day_goes_first(self):
        old = [{"version": "1.0.1", "published": "2026-11-02"}, {"version": "1.0.0", "published": "2026-11-01"}]
        versions = sync.with_version(old, {"version": "1.1.0", "published": "2026-11-02"})
        self.assertEqual([v["version"] for v in versions], ["1.1.0", "1.0.1", "1.0.0"])

    def test_validate_only_assets_of_the_entrys_repository(self):
        self.entry()
        versions = self.root / "branch" / "versions"
        for url in (
            "https://github.com/author/pack/releases/download/../../../other/repo/releases/download/t/pack.zip",
            "https://github.com/author/pack/releases/download/t/pack.zip?x=/pack.zip",
            "https://github.com/author/pack/releases/download/t/sub/pack.zip",
            "https://github.com/author/pack/releases/download/t/other.zip",
        ):
            (versions / "pack.json").write_text(json.dumps({"versions": [{"file": {"name": "pack.zip", "url": url}}]}))
            self.assertIn("pack.json", sync.problems(self.root, versions), url)
        self.cache("pack", ["v1.0+b"])
        self.assertEqual(sync.problems(self.root, versions), {})

    def test_validate_drops_links_and_folders(self):
        self.entry()
        versions = self.root / "branch" / "versions"
        (versions / "pack.json").mkdir()
        (versions / "pack.json" / "x").write_text("{}")
        self.assertIn("pack.json", sync.problems(self.root, versions))
        self.assertEqual(sync.main(["validate", str(self.root), str(versions), "--drop"]), 0)
        self.assertEqual(list(versions.iterdir()), [])
        try:
            (versions / "pack.json").symlink_to(self.root / "mods" / "pack.toml")
        except OSError:
            self.skipTest("no symlinks here")
        self.assertIn("pack.json", sync.problems(self.root, versions))
        sync.main(["validate", str(self.root), str(versions), "--drop"])
        self.assertTrue((self.root / "mods" / "pack.toml").is_file())

    def test_report_lines_never_start_a_runner_command(self):
        self.assertEqual(sync.flat("id: bad\n::set-output name=x::y\n"), "id: bad | ::set-output name=x::y")


if __name__ == "__main__":
    unittest.main()
