import os
import subprocess
import tempfile
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BINARY = '/app/pfc_jsonl'
MAX_MB = 100
MAX_BYTES = MAX_MB * 1024 * 1024


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

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, 'input.jsonl')
        out_path = os.path.join(tmpdir, 'output.pfc')

        with open(in_path, 'wb') as fp:
            fp.write(data)

        result = subprocess.run(
            [BINARY, 'compress', in_path, out_path],
            capture_output=True,
            timeout=180
        )

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')[:300]
            return jsonify({'error': 'Compression failed: ' + stderr}), 500

        orig_name = f.filename or 'archive'
        download_name = orig_name + '.pfc'

        return send_file(
            out_path,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/octet-stream'
        )


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
        in_path = os.path.join(tmpdir, 'input.pfc')
        out_path = os.path.join(tmpdir, 'output.jsonl')

        with open(in_path, 'wb') as fp:
            fp.write(data)

        result = subprocess.run(
            [BINARY, 'decompress', in_path, out_path],
            capture_output=True,
            timeout=180
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
