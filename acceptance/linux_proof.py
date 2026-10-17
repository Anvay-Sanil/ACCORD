"""This gate must execute inside Linux. An unexecuted script is never a passing record."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

from accord.db import utcnow


def main():
    out=Path("acceptance-record/linux");out.mkdir(parents=True,exist_ok=True)
    report={"gate":"docker_linux","timestamp":utcnow().isoformat(),"system":platform.system(),"steps":[],"status":"RUNNING"}
    if platform.system()!="Linux":raise SystemExit("Linux execution required; no emulated PASS")
    commands=[
        [sys.executable,"-m","alembic","upgrade","head"],
        [sys.executable,"-m","pytest","-q","--tb=short","--junitxml="+str(out/"tests.xml")],
        [sys.executable,"-m","acceptance.parser_limits"],
        [sys.executable,"-m","acceptance.process_recovery","--output",str(out/"process-recovery")],
        [sys.executable,"-m","acceptance.browser_proof","--output",str(out/"browser")],
    ]
    for index,command in enumerate(commands):
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=600)
        except subprocess.TimeoutExpired:
            report["steps"].append({"command":command,"error":"TimeoutExpired"})
            report["status"]="FAIL";break
        (out/f"step-{index+1}.log").write_text(result.stdout+result.stderr,encoding="utf-8")
        report["steps"].append({"command":command,"exit_code":result.returncode})
        if result.returncode:report["status"]="FAIL";break
    else:
        suite=ET.parse(out/"tests.xml").getroot().find("testsuite")
        report["tests"]=suite.attrib;report["status"]="PASS"
    (out/"linux-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"]=="PASS" else 1


if __name__=="__main__":raise SystemExit(main())
