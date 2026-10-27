import unittest

from repli_local_ci.redact import Redactor

FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # synthetic, not a real token


class RedactTests(unittest.TestCase):
    def test_github_tokens(self):
        r = Redactor()
        for tok in [FAKE, "github_pat_" + "A" * 40, "ghs_" + "b" * 36, "gho_" + "c" * 36]:
            out = r.line(f"clone with {tok} now")
            self.assertNotIn(tok, out)
            self.assertIn("[REDACTED]", out)

    def test_auth_headers_and_env_assignments(self):
        r = Redactor()
        self.assertNotIn("abc123", r.line("Authorization: Bearer abc123"))
        self.assertNotIn("hunter2", r.line("export DB_PASSWORD=hunter2"))
        self.assertNotIn("zzz", r.line("GITHUB_TOKEN=zzz"))
        self.assertNotIn("x9", r.line("https://user:x9@github.com/a/b"))

    def test_private_key_block_across_lines(self):
        r = Redactor()
        lines = ["-----BEGIN OPENSSH PRIVATE KEY-----", "b3BlbnNzaC1rZXk", "AAAA", "-----END OPENSSH PRIVATE KEY-----", "after"]
        out = [r.line(x) for x in lines]
        self.assertNotIn("b3BlbnNzaC1rZXk", "".join(out))
        self.assertEqual(out[-1], "after")

    def test_literal_secrets(self):
        r = Redactor(literals=["s3cr3t-value"])
        self.assertNotIn("s3cr3t-value", r.line("oops s3cr3t-value leaked"))

    def test_plain_text_untouched(self):
        self.assertEqual(Redactor().line("Ran 12 tests in 1s"), "Ran 12 tests in 1s")
