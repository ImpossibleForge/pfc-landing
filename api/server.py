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

    # Normalize CRLF → LF so DuckDB extension (which strips only \n) sees clean lines
    data = data.replace(b'\r\n', b'\n')

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

    bidx_path = pfc_path + '.bidx'
    has_bidx  = os.path.exists(bidx_path)
    env       = {**os.environ, 'PFC_JSONL_BINARY': BINARY}

    def clean_ts(ts):
        return ts.split('.')[0].rstrip('Z') + 'Z' if '.' in ts else ts

    t0 = time.time()

    if has_bidx:
        # DuckDB extension path — uses BIDX block-level index + read_pfc_jsonl()
        sql = (
            f"LOAD pfc; "
            f"SELECT "
            f"  line->>'$.{ts_field}'    AS timestamp, "
            f"  line->>'$.level'         AS level, "
            f"  line->>'$.service'       AS service, "
            f"  line->>'$.method'        AS method, "
            f"  line->>'$.path'          AS path, "
            f"  line->>'$.status'        AS status, "
            f"  line->>'$.duration_ms'   AS duration_ms, "
            f"  line->>'$.message'       AS message "
            f"FROM read_pfc_jsonl('{pfc_path}') "
            f"WHERE line->>'$.{ts_field}' >= '{clean_ts(from_ts)}' "
            f"  AND line->>'$.{ts_field}' <= '{clean_ts(to_ts)}' "
            f"LIMIT 200;"
        )
        result = subprocess.run(
            [DUCKDB, '-json', '-c', sql],
            capture_output=True, timeout=60, env=env
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        if result.returncode != 0:
            # Fall through to CLI path on extension failure
            has_bidx = False

    if not has_bidx:
        # Fallback: pfc_jsonl query CLI (works without .bidx, embedded index in v5.6.5)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_jsonl = os.path.join(tmpdir, 'out.jsonl')
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

            elapsed_ms = int((time.time() - t0) * 1000)

            rows = []
            if os.path.exists(out_jsonl):
                with open(out_jsonl, encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                            ts_val = str(row.get(ts_field, ''))
                            if ts_val >= from_ts[:19] and ts_val <= to_ts[:19] + 'Z':
                                rows.append(row)
                        except Exception:
                            pass
            rows = rows[:200]

        return jsonify({
            'rows': rows, 'row_count': len(rows),
            'elapsed_ms': elapsed_ms, 'ts_field': ts_field,
            'expires_in': max(0, int(session['expires'] - time.time())),
            'debug': {}, 'query_from': from_ts, 'query_to': to_ts,
            'engine': 'pfc_query_cli',
        })

    # DuckDB path result
    try:
        rows = json.loads(result.stdout.decode('utf-8'))
    except json.JSONDecodeError:
        rows = []

    expires_in = max(0, int(session['expires'] - time.time()))
    debug = {}

    if len(rows) == 0:
        debug['duckdb_stderr'] = result.stderr.decode('utf-8', errors='replace')[:300]

    return jsonify({
        'rows':        rows,
        'row_count':   len(rows),
        'elapsed_ms':  elapsed_ms,
        'ts_field':    ts_field,
        'expires_in':  expires_in,
        'debug':       debug,
        'query_from':  from_ts,
        'query_to':    to_ts,
        'engine': 'duckdb_extension',
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


@app.route('/debug/query/<session_id>', methods=['GET'])
def debug_query(session_id):
    """Debug: run read_pfc_jsonl without WHERE, return first 3 rows raw."""
    if not re.fullmatch(r'[0-9a-f\-]{36}', session_id):
        return jsonify({'error': 'Invalid session_id'}), 400
    session = _read_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    pfc_path  = session['path']
    bidx_path = pfc_path + '.bidx'
    has_bidx  = os.path.exists(bidx_path)
    env = {**os.environ, 'PFC_JSONL_BINARY': BINARY}

    # Test 1: bare read without WHERE
    sql_bare = f"LOAD pfc; SELECT line FROM read_pfc_jsonl('{pfc_path}') LIMIT 3;"
    r1 = subprocess.run([DUCKDB, '-json', '-c', sql_bare],
                        capture_output=True, timeout=30, env=env)

    # Test 2: json extraction test
    sql_json = f"LOAD pfc; SELECT line->>'$.timestamp' AS ts, line->>'timestamp' AS ts2 FROM read_pfc_jsonl('{pfc_path}') LIMIT 3;"
    r2 = subprocess.run([DUCKDB, '-json', '-c', sql_json],
                        capture_output=True, timeout=30, env=env)

    # Test 3: pfc binary version
    r3 = subprocess.run([BINARY, '--version'], capture_output=True, timeout=10)

    # Test 4: run seek-blocks directly (what the extension does internally)
    r4 = subprocess.run(
        [BINARY, 'seek-blocks', '--blocks', '0', '--', pfc_path],
        capture_output=True, timeout=30
    )
    seek_out = r4.stdout.decode('utf-8', errors='replace')

    # Test 5: pfc info (verify file is valid)
    r5 = subprocess.run([BINARY, 'info', pfc_path], capture_output=True, timeout=10)

    # Test 6: exact WHERE query (replicate what /query does)
    ts_field = session.get('ts_field', 'timestamp')
    sql_where = (
        f"LOAD pfc; "
        f"SELECT line->>'$.{ts_field}' AS ts FROM read_pfc_jsonl('{pfc_path}') "
        f"WHERE line->>'$.{ts_field}' >= '2026-06-06T01:00:00Z' "
        f"  AND line->>'$.{ts_field}' <= '2026-06-06T02:00:00Z' LIMIT 5;"
    )
    r6 = subprocess.run([DUCKDB, '-json', '-c', sql_where],
                        capture_output=True, timeout=30, env=env)

    return jsonify({
        'pfc_path':        pfc_path,
        'bidx_exists':     has_bidx,
        'binary_version':  r3.stdout.decode('utf-8', errors='replace').strip(),
        'bare_rc':         r1.returncode,
        'bare_stdout':     r1.stdout.decode('utf-8', errors='replace')[:500],
        'bare_stderr':     r1.stderr.decode('utf-8', errors='replace')[:500],
        'json_rc':         r2.returncode,
        'json_stdout':     r2.stdout.decode('utf-8', errors='replace')[:500],
        'json_stderr':     r2.stderr.decode('utf-8', errors='replace')[:500],
        'seek_rc':         r4.returncode,
        'seek_lines':      len([l for l in seek_out.splitlines() if l.strip()]),
        'seek_first_line': seek_out[:200],
        'seek_stderr':     r4.stderr.decode('utf-8', errors='replace')[:200],
        'pfc_info':        r5.stdout.decode('utf-8', errors='replace')[:300],
        'where_rc':        r6.returncode,
        'where_stdout':    r6.stdout.decode('utf-8', errors='replace')[:300],
        'where_stderr':    r6.stderr.decode('utf-8', errors='replace')[:400],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
