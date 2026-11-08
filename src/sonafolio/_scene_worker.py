"""Private cancellable scene-author process; credentials never enter requests on disk."""
import json
from pathlib import Path
import sys
from .scenes import draft_request
from .paths import write_json

if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text())
    write_json(Path(sys.argv[2]),draft_request(request['keywords']))
