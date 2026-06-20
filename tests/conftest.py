"""Make the src/ layout (and dev tools/) importable in tests."""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
# tools/ holds the PDF-oracle geometry helper shared with the comparison
# harness; tests import it to verify renders against the PDF ground truth.
sys.path.insert(0, str(_ROOT / "tools"))
