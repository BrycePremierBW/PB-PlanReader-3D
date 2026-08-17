# Universal Entrypoint for Streamlit on Render / Cloud Hosting
import os
import sys
import subprocess

if __name__ == '__main__':
    port = os.environ.get('PORT', '8501')
    cmd = [
        sys.executable, '-m', 'streamlit', 'run', 'pb_planreader_app.py',
        '--server.port', str(port),
        '--server.address', '0.0.0.0',
        '--server.headless', 'true',
        '--server.enableCORS', 'false',
        '--server.enableXsrfProtection', 'false',
    ]
    subprocess.run(cmd)
