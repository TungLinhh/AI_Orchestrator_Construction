"""Exercise UIPreferences font behavior: defaults, persistence, invalid fallback.

Runs the real preferences.js + palettes.js in Node with a stubbed DOM and
asserts behavior (stored values round-trip, unknown values fall back, old
stored preferences without `font` keep every other key), not source strings.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/ai_orchestrator/web"

HARNESS = r"""
const assert = require('node:assert/strict');
const store = new Map();
const root = { dataset: {}, style: { setProperty(){} } };
global.window = { localStorage: {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
}, matchMedia: () => null, requestAnimationFrame: (cb) => 0,
  addEventListener(){}, location: { hash: '' } };
global.document = { documentElement: root, addEventListener(){},
  getElementById: () => null };
"""


def run_node(prelude: str, checks: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for UI preference checks")
    script = (
        HARNESS
        + prelude
        + (WEB / "palettes.js").read_text()
        + (WEB / "preferences.js").read_text()
        + checks
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_font_defaults_to_inter():
    run_node(
        "",
        r"""
assert.equal(UIPreferences.get().font, 'inter');
assert.equal(document.documentElement.dataset.font, 'inter');
assert.deepEqual(UIPreferences.fonts.map(f=>f.id), ['inter','system','serif']);
""",
    )


def test_font_persists_and_applies_per_device():
    run_node(
        "",
        r"""
UIPreferences.save({...UIPreferences.get(), font: 'serif'});
assert.equal(UIPreferences.get().font, 'serif');
assert.equal(document.documentElement.dataset.font, 'serif');
const stored = JSON.parse(store.get('onx-ui-preferences-v1'));
assert.equal(stored.font, 'serif');
""",
    )


_SEED_UNKNOWN_FONT = (
    "store.set('onx-ui-preferences-v1',"
    " JSON.stringify({theme:'dark',palette:'teal',"
    "density:'compact',font:'nope'}));"
)
_SEED_LEGACY_PREFS = (
    "store.set('onx-ui-preferences-v1',"
    " JSON.stringify({theme:'light',palette:'forest',"
    "density:'compact',motion:'reduced',home:'organization'}));"
)


def test_unknown_stored_font_falls_back_without_touching_others():
    run_node(
        _SEED_UNKNOWN_FONT,
        r"""
const prefs = UIPreferences.get();
assert.equal(prefs.font, 'inter');
assert.equal(prefs.theme, 'dark');
assert.equal(prefs.palette, 'teal');
assert.equal(prefs.density, 'compact');
assert.equal(document.documentElement.dataset.font, 'inter');
""",
    )


def test_legacy_stored_preferences_gain_font_default():
    run_node(
        _SEED_LEGACY_PREFS,
        r"""
const prefs = UIPreferences.get();
assert.equal(prefs.font, 'inter');
assert.equal(prefs.theme, 'light');
assert.equal(prefs.palette, 'forest');
assert.equal(prefs.density, 'compact');
assert.equal(prefs.motion, 'reduced');
assert.equal(prefs.home, 'organization');
""",
    )


def test_font_save_keeps_theme_accent_density_independent():
    run_node(
        "",
        r"""
UIPreferences.save({...UIPreferences.get(), theme:'dark',
  palette:'teal', density:'compact', font:'system'});
let prefs = UIPreferences.get();
assert.equal(prefs.theme, 'dark');
assert.equal(prefs.palette, 'teal');
assert.equal(prefs.density, 'compact');
assert.equal(prefs.font, 'system');
UIPreferences.save({...prefs, font:'serif'});
prefs = UIPreferences.get();
assert.equal(prefs.theme, 'dark');
assert.equal(prefs.palette, 'teal');
assert.equal(prefs.density, 'compact');
assert.equal(prefs.font, 'serif');
""",
    )
