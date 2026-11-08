"""Isolate network clients and credentials from the desktop control process."""
import sys
from pathlib import Path
from .model_manager import download_in_worker, ModelError
from .paths import write_json

if __name__=='__main__':
    identifier,status=sys.argv[1],Path(sys.argv[2])
    try:download_in_worker(identifier,status)
    except Exception as error:
        # No raw HTTP exception, URL, header or token is written to logs/IPC.
        write_json(status,{'stage':'model_download','state':'failed','model_id':identifier,
                          'error_code':error.code if isinstance(error,ModelError) else 'DOWNLOAD_FAILED'})
