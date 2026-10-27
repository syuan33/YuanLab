import base64
import hashlib

from local_ci_runner import gitops, safety
from tests.fixtures import Remote, TmpCase, git


class ExactSha(TmpCase):
    def test_branch_moving_does_not_change_target(self):
        r = Remote(self.tmp)
        pinned = gitops.resolve_ref(r.url, "main")
        r.commit_verify("echo moved")  # branch advances after pinning
        src = self.tmp / "src"
        src.mkdir()
        gitops.fetch_exact(src, r.url, pinned)
        self.assertEqual(git("rev-parse", "HEAD", cwd=src), pinned)
        self.assertNotEqual(pinned, gitops.resolve_ref(r.url, "main"))

    def test_unknown_sha_fails(self):
        r = Remote(self.tmp)
        src = self.tmp / "src"
        src.mkdir()
        with self.assertRaises(gitops.GitError):
            gitops.fetch_exact(src, r.url, "0" * 40)

    def test_resolve_refuses_non_branch_refs(self):
        r = Remote(self.tmp)
        for bad in ["refs/pull/1/head", "refs/tags/v1", "../x", "main;rm -rf", "-x", ""]:
            with self.assertRaises((gitops.GitError, safety.SafetyError), msg=bad):
                gitops.resolve_ref(r.url, bad)

    def test_nonempty_dest_refused(self):
        r = Remote(self.tmp)
        sha = gitops.resolve_ref(r.url, "main")
        src = self.tmp / "src"
        src.mkdir()
        (src / "leftover").write_text("x")
        with self.assertRaises(safety.SafetyError):
            gitops.fetch_exact(src, r.url, sha)


class Identity(TmpCase):
    def test_parse_remote_identity(self):
        p = gitops.parse_remote_identity
        self.assertEqual(p("git@github.com:example-org/example-repo.git"), "example-org/example-repo")
        self.assertEqual(p("https://github.com/example-org/example-repo"), "example-org/example-repo")
        self.assertIsNone(p("git@evil.com:example-org/example-repo.git"))
        self.assertIsNone(p("/tmp/x/example-repo.git"))
        self.assertIsNone(p("https://github.com.evil.com/example-org/example-repo"))

    def test_identity_fail_closed(self):
        r = Remote(self.tmp)
        sha = gitops.resolve_ref(r.url, "main")
        src = self.tmp / "src"
        src.mkdir()
        gitops.fetch_exact(src, r.url, sha)
        gitops.verify_identity(src, "example-org/example-repo", strict=False)  # synthetic local remote
        with self.assertRaises(gitops.GitError):
            gitops.verify_identity(src, "example-org/example-repo", strict=True)  # local path is not github
        git("remote", "set-url", "origin", "git@github.com:mallory/example-repo.git", cwd=src)
        with self.assertRaises(gitops.GitError):
            gitops.verify_identity(src, "example-org/example-repo", strict=True)


class Submodules(TmpCase):
    def _gm(self, url):
        src = self.tmp / "s"
        src.mkdir(exist_ok=True)
        (src / ".gitmodules").write_text(f'[submodule "m"]\n\tpath = m\n\turl = {url}\n')
        return src

    def test_none_ok(self):
        src = self.tmp / "n"
        src.mkdir()
        gitops.check_submodules(src, frozenset())

    def test_unlisted_submodule_refused(self):
        with self.assertRaises(gitops.GitError):
            gitops.check_submodules(self._gm("git@github.com:example-org/secret-priv.git"), frozenset())

    def test_listed_submodule_ok(self):
        gitops.check_submodules(self._gm("git@github.com:example-org/priv.git"), frozenset({"example-org/priv"}))

    def test_hostile_urls_refused(self):
        for u in ["../relative.git", "file:///etc", "ext::sh -c id", "https://evil.com/example-org/priv.git", "git@github.com:other/priv.git"]:
            with self.assertRaises(gitops.GitError, msg=u):
                gitops.check_submodules(self._gm(u), frozenset({"example-org/priv"}))


class HostKey(TmpCase):
    def test_pinned_key_matches_published_fingerprint(self):
        blob = base64.b64decode(gitops.GITHUB_ED25519_KEY)
        fp = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual("SHA256:" + fp, gitops.GITHUB_ED25519_FINGERPRINT)

    def test_known_hosts_created_then_mismatch_fails_closed(self):
        kh = self.tmp / "known_hosts"
        gitops.ensure_known_hosts(kh)
        gitops.ensure_known_hosts(kh)  # idempotent
        kh.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVILEVILEVILEVILEVILEVILEVILEVILEVILEVILEVIL\n")
        with self.assertRaises(gitops.GitError):
            gitops.ensure_known_hosts(kh)

    def test_scan_mismatch(self):
        good = f"github.com ssh-ed25519 {gitops.GITHUB_ED25519_KEY}\n"
        gitops.verify_scanned_host_key(good)
        for bad in ["github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVIL\n", "", "# only comment\n"]:
            with self.assertRaises(gitops.GitError):
                gitops.verify_scanned_host_key(bad)
