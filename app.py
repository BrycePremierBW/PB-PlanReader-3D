# Universal Entrypoint for Streamlit on Render / Cloud Hosting
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

if __name__ == '__main__':
    port = os.environ.get('PORT', '8501')
    cmd = f'{sys.executable} -m streamlit run pb_planreader_app.py --server.port={port} --server.address=0.0.0.0 --server.headless=true'
    os.system(cmd)
else:
    import pb_planreader_app
