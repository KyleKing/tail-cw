"""Put the Lambda source on the path so the scenario model can be tested without packaging it.

Bytecode writing stays off because `src/` is zipped verbatim by `archive_file`, and a stray
`__pycache__` would change the deployment hash on every test run.
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent / 'src'))
