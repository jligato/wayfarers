"""Real client + server acceptance for the death / legacy / rebirth flow.

Runs the production Godot DeathScreen scene against a live loopback FastAPI
server in an isolated memory mode (no PostgreSQL, no existing player data) and
asserts the client displays the server's legacy award, honours replay, and
applies the carried bonuses. Server-side durability is covered separately by
server/check_legacy_persistence.py.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PYTHON = Path('C:/Users/Boss/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe')

with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]

env = {k: v for k, v in os.environ.items() if not k.startswith(('WC_PG_', 'PG', 'DATABASE_URL'))}
env['PYTHONPATH'] = str(ROOT)
env['WC_TEST_URL'] = f'http://127.0.0.1:{port}'

with tempfile.TemporaryDirectory(prefix='wc_death_') as folder:
    dest = Path(folder)
    shutil.copytree(ROOT / 'client/scenes', dest / 'scenes')
    shutil.copytree(ROOT / 'client/scripts', dest / 'scripts')
    shutil.copy2(HERE / 'drive.gd', dest / 'drive.gd')
    config = (ROOT / 'client/project.godot').read_text(encoding='utf-8')
    config = config.replace('config/name="Wayfarer\'s Cycle"', 'config/name="' + dest.name + '"')
    config = '\n'.join(line for line in config.splitlines() if not line.startswith('config/icon='))
    (dest / 'project.godot').write_text(config, encoding='utf-8')

    # Autoload/typed class declarations live in .godot/global_script_class_cache.cfg.
    # Without it Godot cannot resolve types such as ZoneSystem, PrestigeSystem and
    # NPCSystem, and every scene using them fails to parse with
    # "Could not find type ... in the current scope". Working harnesses
    # (session_integration, hub_views/run_views_repair) copy this file.
    src_cache = ROOT / 'client/.godot/global_script_class_cache.cfg'
    if not src_cache.exists():
        raise RuntimeError('global_script_class_cache.cfg missing from client/.godot')
    (dest / '.godot').mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_cache, dest / '.godot/global_script_class_cache.cfg')

    with (HERE / 'server.log').open('w', encoding='utf-8') as logfile:
        server = subprocess.Popen(
            [str(PYTHON), '-m', 'uvicorn', 'server.main:app', '--host', '127.0.0.1', '--port', str(port)],
            cwd=ROOT, env=env, stdout=logfile, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 25
            while True:
                try:
                    with urllib.request.urlopen(env['WC_TEST_URL'] + '/health', timeout=1) as response:
                        assert response.status == 200
                    break
                except Exception:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('isolated server did not become ready; see server.log')
                    time.sleep(.1)

            result = subprocess.run(
                ['godot', '--headless', '--path', str(dest), '--script', 'res://drive.gd'],
                env=env, capture_output=True, text=True, timeout=120)
            output = result.stdout + result.stderr
            (HERE / 'result.log').write_text(output, encoding='utf-8')
            print(output)
            if 'DEATH_LEGACY_UI_RESULT' not in output:
                raise RuntimeError('death driver produced no result line')
            if 'SCRIPT ERROR' in output:
                raise RuntimeError('driver reported a script error')
            if '"failures":[]' not in output:
                raise RuntimeError('death/legacy acceptance failed')
            print('PASS: production DeathScreen, server-authoritative legacy award, replay guard '
                  'and legacy apply; isolated memory server only')
        finally:
            server.terminate()
            server.wait(timeout=15)
            userdata = Path(os.environ['APPDATA']) / 'Godot/app_userdata' / dest.name
            if userdata.is_dir():
                shutil.rmtree(userdata)
