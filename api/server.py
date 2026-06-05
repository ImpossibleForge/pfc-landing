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

# Sessions stored on filesystem — safe across gunicorn workers and restarts
def _session_meta_path(sid: str) -> str:
    return os.path.join(SESSION_DIR, sid + '.json')

def _session_pfc_path(sid: str) -> str:
    return os.path.join(SESSION_DIR, sid + '.pfc')

def _write_session(sid: str, meta: dict):
    with open(_session_meta_path(sid), 'w') as f:
        json.dump(meta, f)

def _read_session(sid: str) -> dict | None:
    try:
        with open(_session_meta_path(sid)) as f:
            return json.load(f)
    except Exception:
        return None

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
        for fname in os.listdir(SESSION_DIR):
            if not fname.endswith('.json'):
                continue
            sid = fname[:-5]
            meta = _read_session(sid)
            if meta and meta.get('expires', 0) < now:
                for ext in ('.pfc', '.json'):
                    try:
                        os.remove(os.path.join(SESSION_DIR, sid + ext))
                    except OSError:
                        pass


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

        # Save session copy (filesystem — survives across workers)
        session_id   = str(uuid.uuid4())
        session_path = _session_pfc_path(session_id)
        shutil.copy2(out_path, session_path)
        # Copy .bidx index if present (required by pfc-duckdb extension)
        bidx_src = out_path + '.bidx'
        if os.path.exists(bidx_src):
            shutil.copy2(bidx_src, session_path + '.bidx')
        expires_at = time.time() + SESSION_TTL

        _write_session(session_id, {
            'path':     session_path,
            'expires':  expires_at,
            'ts_field': ts_field,
            'ts_min':   ts_min,
            'ts_max':   ts_max,
        })

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

    session = _read_session(session_id)

    if not session:
        return jsonify({'error': 'Session not found or expired'}), 404

    if time.time() > session['expires']:
        return jsonify({'error': 'Session expired'}), 410

    pfc_path = session['path']
    ts_field = session['ts_field']

    # Use pfc_jsonl query CLI directly — works with v5.6.5 native BIDX index.
    # The DuckDB community extension targets v3.4 CLI syntax and is not compatible
    # with the v5.6.5 Rust rewrite. Showcase the DuckDB extension as a desktop option.
    t0 = time.time()

    with tempfile.TemporaryDirectory() as tmpdir:
        out_jsonl = os.path.join(tmpdir, 'out.jsonl')

        # Strip milliseconds from ISO string if present — pfc_jsonl query is strict
        def clean_ts(ts):
            return ts.split('.')[0].rstrip('Z') + 'Z' if '.' in ts else ts

        q_result = subprocess.run(
            [BINARY, 'query', pfc_path,
             '--from', clean_ts(from_ts),
             '--to',   clean_ts(to_ts),
             '--out',  out_jsonl],
            capture_output=True, timeout=60
        )

        if q_result.returncode != 0:
            stderr = q_result.stderr.decode('utf-8', errors='replace')[:400]
            return jsonify({'error': 'Query failed: ' + stderr}), 500

        # Parse extracted JSONL into rows
        rows = []
        if os.path.exists(out_jsonl):
            with open(out_jsonl, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        rows = rows[:200]

    elapsed_ms = int((time.time() - t0) * 1000)

    expires_in = max(0, int(session['expires'] - time.time()))
    debug = {}

    # If empty result — show CLI stderr as hint
    if len(rows) == 0:
        debug['cli_stderr'] = q_result.stderr.decode('utf-8', errors='replace')[:300]
        debug['query_cmd'] = f"pfc_jsonl query ... --from {from_ts} --to {to_ts}"

    return jsonify({
        'rows':        rows,
        'row_count':   len(rows),
        'elapsed_ms':  elapsed_ms,
        'ts_field':    ts_field,
        'expires_in':  expires_in,
        'debug':       debug,
        'query_from':  from_ts,
        'query_to':    to_ts,
        'engine':      'pfc_query_bidx',
    })


@app.route('/session/<session_id>', methods=['GET'])
def session_info(session_id):
    if not re.fullmatch(r'[0-9a-f\-]{36}', session_id):
        return jsonify({'error': 'Invalid session_id'}), 400
    session = _read_session(session_id)
    if not session or time.time() > session['expires']:
        return jsonify({'alive': False}), 200
    return jsonify({
        'alive':      True,
        'expires_in': max(0, int(session['expires'] - time.time())),
        'ts_field':   session['ts_field'],
        'ts_min':     session.get('ts_min'),
        'ts_max':     session.get('ts_max'),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
