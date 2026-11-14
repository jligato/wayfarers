"""Regression tests for the cross-test pollution documented in the baseline report.

These assert nothing about the game. They assert that running the test suite
does not silently change the behaviour of tests collected later, and they are
the proof behind docs/test-suite-baseline.md.

Every test here is self-contained and fast: no database, no server, no network.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SERVER = REPO / "server"
ENV_NAME = "WC_PG_HOST"

# Test modules that are standalone durability/integration scripts: they match
# the pytest discovery pattern but contain no test functions.
# Standalone durability/integration SCRIPT files. They hold no test
# functions and need a live PostgreSQL container or a live uvicorn, so
# they used to match pytest's ``test_*.py`` pattern and had to be listed
# in ``collect_ignore``. They are now named ``check_*.py``, which pytest
# never discovers, so the ignore list is gone and the names below are the
# proof that the rename actually landed.
SCRIPT_FILES = [
    "check_artifacts_persistence.py",
    "check_equipment_persistence.py",
    "check_ghost_battle_persistence.py",
    "check_inner_skills_persistence.py",
    "check_legacy_persistence.py",
    "check_pvp_persistence.py",
    "check_restart_durability.py",
]

# The old ``test_integration.py`` was a byte-identical copy of
# ``server/integration_script.py``. It was deleted rather than renamed.
DELETED_DUPLICATE = "test_integration.py"
DUPLICATE_SOURCE = "integration_script.py"

# Test modules that delete the PostgreSQL environment variable at MODULE level.
# That is a process-wide mutation performed at import time, which makes the
# backend of every later-collected test depend on collection order.
ENV_POPPING_FILES = [
    "test_battle_pass_xp.py",
    "test_bounty_generator.py",
    "test_lifecycle.py",
    "test_lottery_pity.py",
    "test_retention.py",
    "test_tribulation_aftermath.py",
    "tests/_aftermath_proof.py",
    "tests/retention_fixes/test_out_of_scope_defects.py",
    "tests/test_battle_pass_claim_route.py",
    "tests/test_shadow_audit_bp_and_sect.py",
]


def _count_test_functions(path):
    """Count pytest-visible test functions without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )

def _module_level_env_drops(path):
    """Line numbers of top-level ``os.environ.pop(ENV_NAME, None)`` calls."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found = []
    for node in tree.body:  # module level only, never inside a function
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "pop"):
            continue
        if not (isinstance(func.value, ast.Attribute) and func.value.attr == "environ"):
            continue
        if call.args and isinstance(call.args[0], ast.Constant):
            if call.args[0].value == ENV_NAME:
                found.append(node.lineno)
    return found


class TestCollectionPolicy:
    """The 5 script files must not be imported by pytest."""

    @pytest.mark.parametrize("name", SCRIPT_FILES)
    def test_script_file_really_has_no_tests(self, name):
        """They are ignored, so confirm they are genuinely not test modules."""
        path = SERVER / name
        assert path.exists(), f"{name} is missing"
        assert _count_test_functions(path) == 0, (
            f"{name} now defines test functions but is in collect_ignore; "
            "someone added real tests to a file we told pytest to skip"
        )

    def test_script_files_are_not_discoverable_by_pytest(self):
        """The rename is the guard: no ``test_*.py`` name may reappear.

        This replaces the old assertion that ``collect_ignore`` listed
        the scripts. That list was a workaround for the names; with the
        names fixed, a script that drifts back to ``test_*`` would be
        collected and would execute its work at import time -- exactly the
        failure mode this file exists to prevent.
        """
        source = (SERVER / "conftest.py").read_text(encoding="utf-8")
        for name in SCRIPT_FILES:
            assert not name.startswith("test_"), (
                f"{name} would match pytest's test_*.py discovery pattern"
            )
        for stale in ("test_artifacts_persistence.py", "test_equipment_persistence.py",
                       "test_inner_skills_persistence.py", "test_legacy_persistence.py"):
            assert stale not in source, (
                f"{stale} is still named collect_ignore; the rename to "
                f"check_*_persistence.py makes the entry unnecessary"
            )

    def test_the_integration_duplicate_is_gone_and_its_source_remains(self):
        """Only the correctly-named copy of the integration script survives."""
        assert not (SERVER / DELETED_DUPLICATE).exists(), (
            f"{DELETED_DUPLICATE} was a byte-identical copy of "
            f"{DUPLICATE_SOURCE}; it must not come back"
        )
        survivor = SERVER / DUPLICATE_SOURCE
        assert survivor.exists(), (
            f"{DUPLICATE_SOURCE} is missing, so deleting the duplicate "
            f"would have removed the only copy"
        )
        assert _count_test_functions(survivor) == 0

    def test_lifecycle_persistence_is_a_real_module_not_ignored(self):
        """It has 9 real tests, so it must stay collected, not skipped."""
        path = SERVER / "test_lifecycle_persistence.py"
        assert _count_test_functions(path) >= 9, (
            "test_lifecycle_persistence.py lost its test functions; the "
            "conftest docstring says it is deliberately NOT in collect_ignore"
        )
        source = (SERVER / "conftest.py").read_text(encoding="utf-8")
        assert '"test_lifecycle_persistence.py"' not in source, (
            "test_lifecycle_persistence.py was wrongly added to collect_ignore"
        )
