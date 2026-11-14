"""
Contract tests for tools/prepush_secret_gate.py — the pre-push secret gate.

WHAT THIS PROVES
----------------
The gate refuses a push whose HISTORY contains a credential file. Its output
is two machine-readable facts and nothing else:

    exit 0 + "PASS"    -> safe to push
    exit 1 + "REFUSED" -> blocked, and the paths are named on stderr

A gate that prints REFUSED unconditionally would also look "safe" in a demo,
and a gate that prints PASS unconditionally would look broken. So every test
below BUILDS A REAL THROW-AWAY GIT REPOSITORY, commits real files into it,
then runs the real script as a subprocess with that directory as its working
directory. The script shells out to `git log --all ... --diff-filter=A`, so
its verdict is a function of the repository it is standing in.

THE THREE ARMS
--------------
1. CLEAN    -> ordinary source files. Must PASS. This arm is the control: if
               the gate refuses ordinary source, it is unusable, not strict.
2. KEYSTORE -> keys/release.keystore. A PKCS#12 release signing key. Refused.
3. BUILDPASS-> keys/.buildpass, the plaintext build password next to it.
               Refused.

Arms 2 and 3 also assert the reported path, because a gate that refuses for
an unspecified reason still stops the push but leaves the human guessing.
Both arms additionally prove the file is committed and tracked: `git ls-files`
must name it, which is what makes this a HISTORY finding rather than a
working-tree one.

Arm 2's file is a plain-text placeholder, NOT a real keystore. The gate
matches on FILENAME by design (see the module docstring: on this project the
keystore password is a substring of the public Android package id, so a
value scan produces permanent false alarms). Nothing here ever runs keytool
or parses a PKCS#12 container.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# server/tests/test_prepush_gate_contract.py -> repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "prepush_secret_gate.py"

pytestmark = pytest.mark.skipif(
    not GATE.exists(),
    reason="tools/prepush_secret_gate.py is not present in this checkout",
)


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise AssertionError(
            "git %s failed (%d): %s" % (" ".join(args), r.returncode, r.stderr)
        )
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path):
    """A throw-away git repo with one ordinary commit already in it."""
    d = tmp_path / "repo"
    d.mkdir()
    _git("init", "-q", cwd=d)
    _git("config", "user.email", "gate-contract@example.invalid", cwd=d)
    _git("config", "user.name", "Gate Contract Test", cwd=d)
    _git("config", "commit.gpgsign", "false", cwd=d)
    # A clean starting point: one ordinary source file, committed.
    (d / "README.md").write_text("clean repo\n", encoding="utf-8")
    _git("add", "README.md", cwd=d)
    _git("commit", "-q", "-m", "clean base", cwd=d)
    return d


def _run_gate(repo_dir: Path) -> subprocess.CompletedProcess:
    """Run the real gate script with repo_dir as its working directory."""
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _commit(d: Path, rel: str, body: str) -> None:
    """Write a file and commit it, so it lands in the scanned history."""
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    _git("add", "--", rel, cwd=d)
    _git("commit", "-q", "-m", "add %s" % rel, cwd=d)


# ---------------------------------------------------------------------------
# 1. The control arm: ordinary source in history must PASS.
# ---------------------------------------------------------------------------

def test_clean_history_passes(repo: Path) -> None:
    _commit(repo, "src/app.py", "print('hello')\n")
    _commit(repo, "docs/notes.md", "no credentials here\n")

    r = _run_gate(repo)
    out = r.stdout + r.stderr

    assert r.returncode == 0, "clean history must exit 0, got %d\n%s" % (
        r.returncode, out)
    assert "PASS" in out, "clean history must print PASS, got:\n%s" % out
    assert "REFUSED" not in out, (
        "clean history was refused; the gate is not usable as-is\n%s" % out)


# ---------------------------------------------------------------------------
# 2. A release signing keystore in history must REFUSE.
# ---------------------------------------------------------------------------

def test_release_keystore_in_history_is_refused(repo: Path) -> None:
    # Placeholder bytes, real FILENAME. The gate matches the name on purpose.
    _commit(repo, "keys/release.keystore", "PKCS12 PLACEHOLDER - not a real key\n")

    tracked = _git("ls-files", cwd=repo)
    assert "keys/release.keystore" in tracked, (
        "the keystore must be committed and tracked for this to be a history "
        "finding; git ls-files said: %r" % tracked)

    r = _run_gate(repo)
    out = r.stdout + r.stderr

    assert r.returncode == 1, "a committed keystore must exit 1, got %d\n%s" % (
        r.returncode, out)
    assert "REFUSED" in out, "expected REFUSED on stderr, got:\n%s" % out
    assert "PASS" not in out, "a refusal must not also claim PASS\n%s" % out
    assert "keys/release.keystore" in r.stderr, (
        "the refusal must name the offending path, got:\n%s" % r.stderr)
    assert ".keystore" in r.stderr, (
        "the refusal must state which rule matched, got:\n%s" % r.stderr)


# ---------------------------------------------------------------------------
# 3. A plaintext build password in history must REFUSE.
# ---------------------------------------------------------------------------

def test_buildpass_in_history_is_refused(repo: Path) -> None:
    _commit(repo, "keys/.buildpass", "hunter2-not-a-real-password\n")

    tracked = _git("ls-files", cwd=repo)
    assert "keys/.buildpass" in tracked, (
        "the buildpass must be committed and tracked; git ls-files said: %r"
        % tracked)

    r = _run_gate(repo)
    out = r.stdout + r.stderr

    assert r.returncode == 1, "a committed .buildpass must exit 1, got %d\n%s" % (
        r.returncode, out)
    assert "REFUSED" in out, "expected REFUSED on stderr, got:\n%s" % out
    assert "PASS" not in out, "a refused gate must not also print PASS, got:\n%s" % out
    assert "keys/.buildpass" in r.stderr, (
        "the refusal must name the offending path, got:\n%s" % r.stderr)


# ---------------------------------------------------------------------------
# 4. The matcher itself, called directly.
#
# Arms 1-3 prove the gate's VERDICT, but every one of them reaches the matcher
# only through the real `git log` history scan, so a matcher that matched
# nothing would turn them all into the PASS arm and they would still look
# green. These call is_credential() directly with synthetic paths -- no git
# repository, no subprocess -- so a matcher that stops matching is caught here
# instead of hiding behind three passing integration arms.
#
# Each negative assertion has a positive twin. That pairing is the point: a
# matcher that returned a match for everything would pass a lone "is not None"
# check on a credential path, and a matcher that returned None for everything
# would pass a lone "is None" check on a clean path. Only the pair distinguishes
# "matches exactly the credentials" from "always answers the same way".
# ---------------------------------------------------------------------------

def _load_gate():
    """Import tools/prepush_secret_gate.py as a module, by file path."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("prepush_secret_gate_under_test", GATE)
    assert spec is not None and spec.loader is not None, (
        "could not build an import spec for %s" % GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def test_matcher_reports_a_match_for_a_keystore_path(gate) -> None:
    """A synthetic credential path must be reported as a match."""
    why = gate.is_credential("keys/release.keystore")
    assert why is not None, (
        "is_credential did not flag keys/release.keystore; the gate would pass "
        "a history containing it")
    assert ".keystore" in why, (
        "the reported reason must name the rule that matched, got %r" % why)


def test_matcher_reports_a_match_for_a_buildpass_path(gate) -> None:
    """The plaintext build password is matched by name too."""
    why = gate.is_credential("keys/.buildpass")
    assert why is not None, "is_credential did not flag keys/.buildpass"
    assert why, "a match must carry a non-empty reason string"


def test_matcher_does_not_flag_an_ordinary_source_path(gate) -> None:
    """The control for the two arms above: a clean path reports no match."""
    assert gate.is_credential("src/app.py") is None, (
        "ordinary source was flagged as a credential; the gate is unusable")
    assert gate.is_credential("docs/release-notes.md") is None, (
        "an ordinary doc was flagged as a credential; the gate is unusable")


def test_matcher_honours_the_allowlist_over_the_glob(gate) -> None:
    """An allowlisted path is clean even though its name looks like a secret.

    `admin/wcadmin/test_secret_key.py` ends in `_key.py` but is a test that
    asserts the admin refuses a bad secret. Without the allowlist the gate
    would block a legitimate commit forever.
    """
    allowlisted = "admin/wcadmin/test_secret_key.py"
    assert allowlisted in gate.ALLOW, (
        "the allowlist no longer holds %s; this test is measuring nothing"
        % allowlisted)
    assert gate.is_credential(allowlisted) is None, (
        "an allowlisted path was reported as a credential; the allowlist is "
        "not being applied before the filename match")
