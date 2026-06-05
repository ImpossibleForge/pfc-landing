import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from flask import Flask, after_this_request, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, expose_headers=['X-Session-Id', 'X-Session-Expires', 'X-Ts-Field', 'X-Ts-Min', 'X-Ts-Max'])

BINARY  = '/app/pfc_jsonl'
DUCKDB  = '/usr/local/bin/duckdb'
MAX_MB  = 100
MAX_BYTES = MAX_MB * 1024 * 1024
SESSION_TTL = 900  # 15 minutes
SESSION_DIR = '/tmp/pfc_sessions'

os.makedirs(SESSION_DIR, exist_ok=True)

# {session_id: {"path": str, "expires": float, "ts_field": str}}
_sessions: dict = {}
_lock = threading.Lock()

TS_CANDIDATES = ['timestamp', 'ts', 'time', '@timestamp', 'date', 'datetime']


def _detect_ts_field(jsonl_bytes: bytes) -> str:
    try:
        first_line = jsonl_bytes.split(b'\n')[0]
        row = json.loads(first_line)
        for f in TS_CANDIDATES:
            if f in row:
                return f
    except Exception:
        pass
    return 'timestamp'


def _detect_ts_range(jsonl_bytes: bytes, ts_field: str) -> tuple:
    """Sample first+last 20 lines to find min/max timestamp strings."""
    lines = [l for l in jsonl_bytes.split(b'\n') if l.strip()]
    sample = lines[:20] + lines[-20:]
    values = []
    for l in sample:
        try:
            v = json.loads(l).get(ts_field)
            if v is not None:
                values.append(str(v))
        except Exception:
            pass
    if not values:
        return None, None
    values.sort()
    return values[0], values[-1]


def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with _lock:
            expired = [sid for sid, s in _sessions.items() if s['expires'] < now]
            for sid in expired:
                try:
                    os.remove(_sessions[sid]['path'])
                except OSError:
                    pass
                del _sessions[sid]


threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'binary': BINARY, 'max_mb': MAX_MB})


@app.route('/compress', methods=['POST'])
def compress():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    data = f.read()

    if len(data) == 0:
        return jsonify({'error': 'Empty file'}), 400
    if len(data) > MAX_BYTES:
        return jsonify({'error': f'File too large. Maximum is {MAX_MB} MB.'}), 413

    ts_field = _detect_ts_field(data)
    ts_min, ts_max = _detect_ts_range(data, ts_field)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path  = os.path.join(tmpdir, 'input.jsonl')
        out_path = os.path.join(tmpdir, 'output.pfc')

        with open(in_path, 'wb') as fp:
            fp.write(data)

        result = subprocess.run(
            [BINARY, 'compress', in_path, out_path],
            capture_output=True, timeout=180
        )

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')[:300]
            return jsonify({'error': 'Compression failed: ' + stderr}), 500

        # Save session copy
        session_id   = str(uuid.uuid4())
        session_path = os.path.join(SESSION_DIR, session_id + '.pfc')
        shutil.copy2(out_path, session_path)
        expires_at = time.time() + SESSION_TTL

        with _lock:
            _sessions[session_id] = {
                'path':     session_path,
                'expires':  expires_at,
                'ts_field': ts_field,
                'ts_min':   ts_min,
                'ts_max':   ts_max,
            }

        orig_name     = f.filename or 'archive'
        download_name = orig_name + '.pfc'

        response = send_file(
            out_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/octet-stream'
        )
        response.headers['X-Session-Id']      = session_id
        response.headers['X-Session-Expires'] = str(int(expires_at))
        response.headers['X-Ts-Field']        = ts_field
        if ts_min:
            response.headers['X-Ts-Min'] = ts_min
        if ts_max:
            response.headers['X-Ts-Max'] = ts_max
        return response


@app.route('/decompress', methods=['POST'])
def decompress():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    data = f.read()

    if len(data) == 0:
        return jsonify({'error': 'Empty file'}), 400
    if len(data) > MAX_BYTES:
        return jsonify({'error': f'File too large. Maximum is {MAX_MB} MB.'}), 413

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path  = os.path.join(tmpdir, 'input.pfc')
        out_path = os.path.join(tmpdir, 'output.jsonl')

        with open(in_path, 'wb') as fp:
            fp.write(data)

        result = subprocess.run(
            [BINARY, 'decompress', in_path, out_path],
            capture_output=True, timeout=180
        )

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')[:300]
            return jsonify({'error': 'Decompression failed: ' + stderr}), 500

        orig_name = f.filename or 'output.jsonl'
        if orig_name.endswith('.pfc'):
            orig_name = orig_name[:-4]
        if not orig_name.endswith('.jsonl'):
            orig_name += '.jsonl'

        return send_file(
            out_path,
            as_attachment=True,
            download_name=orig_name,
            mimetype='application/x-ndjson'
        )


@app.route('/query', methods=['POST'])
def query():
    body = request.get_json(silent=True) or {}

    session_id = body.get('session_id', '').strip()
    from_ts    = body.get('from_ts', '').strip()
    to_ts      = body.get('to_ts', '').strip()

    if not session_id or not from_ts or not to_ts:
        return jsonify({'error': 'session_id, from_ts and to_ts are required'}), 400

    # Validate session_id is a UUID (no path traversal)
    if not re.fullmatch(r'[0-9a-f\-]{36}', session_id):
        return jsonify({'error': 'Invalid session_id'}), 400

    # Sanitize timestamps (only allow ISO-ish strings)
    if not re.fullmatch(r'[\d\-T:Z\+\.]{10,35}', from_ts) or \
       not re.fullmatch(r'[\d\-T:Z\+\.]{10,35}', to_ts):
        return jsonify({'error': 'Invalid timestamp format'}), 400

    with _lock:
        session = _sessions.get(session_id)

    if not session:
        return jsonify({'error': 'Session not found or expired'}), 404

    if time.time() > session['expires']:
        return jsonify({'error': 'Session expired'}), 410

    pfc_path = session['path']
    ts_field = session['ts_field']

    # read_pfc_jsonl() returns one 'line' column (raw JSON string per row).
    # Use JSON path extraction for field access and filtering.
    sql = (
        f"LOAD pfc; LOAD json; "
        f"SELECT "
        f"  line->>'$.timestamp' AS timestamp, "
        f"  line->>'$.level'     AS level, "
        f"  line->>'$.service'   AS service, "
        f"  line->>'$.method'    AS method, "
        f"  line->>'$.path'      AS path, "
        f"  line->>'$.status'    AS status, "
        f"  line->>'$.duration_ms' AS duration_ms, "
        f"  line->>'$.message'   AS message "
        f"FROM read_pfc_jsonl('{pfc_path}') "
        f"WHERE line->>'$.{ts_field}' >= '{from_ts}' "
        f"  AND line->>'$.{ts_field}' <= '{to_ts}' "
        f"LIMIT 200;"
    )

    env = {**os.environ, 'PFC_JSONL_BINARY': BINARY}

    t0 = time.time()
    result = subprocess.run(
        [DUCKDB, '-json', '-c', sql],
        capture_output=True, timeout=30, env=env
    )
    elapsed_ms = int((time.time() - t0) * 1000)

    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')[:400]
        return jsonify({'error': 'Query failed: ' + stderr}), 500

    try:
        rows = json.loads(result.stdout.decode('utf-8'))
    except json.JSONDecodeError:
        rows = []

    expires_in = max(0, int(session['expires'] - time.time()))
    debug = {}

    # If empty result — run a debug sample to diagnose
    if len(rows) == 0:
        sql_sample = (
            f"LOAD pfc; LOAD json; "
            f"SELECT line->>'$.{ts_field}' AS ts_value "
            f"FROM read_pfc_jsonl('{pfc_path}') LIMIT 3;"
        )
        r2 = subprocess.run([DUCKDB, '-json', '-c', sql_sample],
                            capture_output=True, timeout=30, env=env)
        try:
            sample = json.loads(r2.stdout.decode('utf-8'))
            debug['sample_rows'] = sample
            if sample:
                debug['actual_ts_value'] = sample[0].get('ts_value', 'field not found')
        except Exception:
            debug['sample_error'] = r2.stderr.decode('utf-8', errors='replace')[:300]
        debug['stderr'] = result.stderr.decode('utf-8', errors='replace')[:300]

    return jsonify({
        'rows':        rows,
        'row_count':   len(rows),
        'elapsed_ms':  elapsed_ms,
        'ts_field':    ts_field,
        'expires_in':  expires_in,
        'debug':       debug,
        'query_from':  from_ts,
        'query_to':    to_ts,
    })


@app.route('/session/<session_id>', methods=['GET'])
def session_info(session_id):
    if not re.fullmatch(r'[0-9a-f\-]{36}', session_id):
        return jsonify({'error': 'Invalid session_id'}), 400
    with _lock:
        session = _sessions.get(session_id)
    if not session or time.time() > session['expires']:
        return jsonify({'alive': False}), 200
    return jsonify({
        'alive':      True,
        'expires_in': max(0, int(session['expires'] - time.time())),
        'ts_field':   session['ts_field'],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
