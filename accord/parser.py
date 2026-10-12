import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class ParseFailure(Exception):
    pass


def parse_pdf(body,wall_seconds=8):
    if len(body)>5_242_880 or not body.startswith(b"%PDF-"):
        raise ParseFailure("Invalid or oversized PDF")
    with tempfile.TemporaryDirectory(prefix="accord-pdf-") as directory:
        source = Path(directory)/"document.pdf"
        source.write_bytes(body)
        # Child inherits no PayPal/database/model secrets. It receives only the temporary PDF path.
        env = {k:v for k,v in os.environ.items() if k in ("SYSTEMROOT","WINDIR","PATH","TEMP","TMP")}
        try:
            result = subprocess.run([sys.executable,"-I",str(Path(__file__).with_name("pdf_child.py")),str(source)],
                cwd=directory,env=env,capture_output=True,timeout=wall_seconds,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise ParseFailure("PDF parser exceeded wall-clock limit") from exc
        if result.returncode or len(result.stdout)>1_048_576:
            raise ParseFailure("PDF parser failed or exceeded OS limits")
        try:
            return json.loads(result.stdout)
        except (ValueError,UnicodeDecodeError) as exc:
            raise ParseFailure("Invalid parser output") from exc

