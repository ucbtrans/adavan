from flask import Flask, render_template, request, jsonify
import subprocess
import os

app = Flask(__name__)

SCRIPT_PATH = "video_collection/video_capture.py"
VEHICLE_KEY = "video/"
OUTPUT_PATH = "RV1"

process = None

def start_recording_process():
    global process
    if process is None:
        try:
            cmd = ["python3", SCRIPT_PATH, "--output_svo_file_path", OUTPUT_PATH, "--vehicle_key", VEHICLE_KEY]
            process = subprocess.Popen(cmd)
            print("Recording started by default.")
            return True
        except Exception as e:
            print(f"Error starting process: {e}")
            return False
    return False


@app.route('/')
def index():
    """Renders the main web page with the control button."""
    return render_template('index.html')


@app.route('/toggle_recording', methods=['POST'])
def toggle_recording():
    global process
    state = request.json.get('state')
    print(state)
    
    if state == 'start':
        start_recording_process()
        return jsonify(status="started")
            
    elif state == 'stop' and process is not None:
        try:
            process.terminate()
            process = None
            print("Stopped")
            return jsonify(status="stopped")
        except Exception as e:
            return jsonify(status="error", message=str(e))
            
    return jsonify(status="no_change")


if __name__ == '__main__':
    app.run(debug=True)