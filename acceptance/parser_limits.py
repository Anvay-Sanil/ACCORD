"""Direct Linux CPU/memory/wall-limit probes using the production parser limiter."""
import json
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

from accord import pdf_child
from accord.db import utcnow
from accord.parser import parse_pdf,ParseFailure
from acceptance.fixtures import pdf_bytes


def main():
    out=Path("acceptance-record/linux");out.mkdir(parents=True,exist_ok=True)
    if platform.system()!="Linux":raise SystemExit("This proof requires Linux")
    prefix=f"import runpy; m=runpy.run_path({str(Path(pdf_child.__file__).resolve())!r}); m['limits'](); "
    report={"timestamp":utcnow().isoformat(),"platform":platform.platform(),"checks":{}}
    memory=subprocess.run([sys.executable,"-I","-c",prefix+"bytearray(700*1024*1024)"],capture_output=True,timeout=15)
    report["checks"]["512mb_memory_limit"]=memory.returncode!=0 and b"MemoryError" in memory.stderr
    start=time.monotonic()
    cpu=subprocess.run([sys.executable,"-I","-c",prefix+"exec('while True: pass')"],capture_output=True,timeout=30)
    report["cpu_probe"]={"returncode":cpu.returncode,"elapsed_seconds":round(time.monotonic()-start,2)}
    report["checks"]["5s_cpu_limit"]=cpu.returncode in (-signal.SIGKILL,-signal.SIGXCPU)
    try:
        parse_pdf(pdf_bytes("PO: PO-001\nLot: LOT-001\nDelivered grade: 316L"),wall_seconds=.001)
        report["checks"]["wall_timeout"]=False
    except ParseFailure as exc:report["checks"]["wall_timeout"]="wall-clock" in str(exc)
    report["status"]="PASS" if all(report["checks"].values()) else "FAIL"
    (out/"parser-limits.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report));return 0 if report["status"]=="PASS" else 1


if __name__=="__main__":raise SystemExit(main())
