"""Record Docker availability; --run executes the isolated Linux acceptance stack."""
import argparse
import json
from pathlib import Path
import subprocess

from accord.db import utcnow


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--run",action="store_true");args=parser.parse_args()
    out=Path("acceptance-record/linux");out.mkdir(parents=True,exist_ok=True)
    report={"timestamp":utcnow().isoformat(),"gate":"docker_linux","status":"BLOCKED"}
    try:
        probe=subprocess.run(["docker","version","--format","{{json .Server}}"],capture_output=True,text=True,timeout=30)
        (out/"docker-probe.log").write_text(probe.stdout+probe.stderr,encoding="utf-8")
        report["probe_exit_code"]=probe.returncode
        if probe.returncode:
            report["reason"]="Docker engine unavailable to this process; see docker-probe.log."
        elif not args.run:
            report.update(status="READY_NOT_EXECUTED",reason="Use --run to execute the Linux stack.")
        else:
            command=["docker","compose","--project-name","accord-acceptance","-f","compose.acceptance.yaml",
                "up","--build","--abort-on-container-exit","--exit-code-from","acceptance"]
            with (out/"compose.log").open("w",encoding="utf-8") as log:
                result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
            report.update(command=command,compose_exit_code=result.returncode)
            result_path=out/"linux-result.json"
            current=json.loads(result_path.read_text()) if result_path.exists() else {}
            report["status"]="PASS" if result.returncode==0 and current.get("status")=="PASS" else "FAIL"
    except (FileNotFoundError,subprocess.TimeoutExpired) as exc:report["reason"]=type(exc).__name__
    (out/"docker-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report));return 0 if report["status"]=="PASS" else 2


if __name__=="__main__":raise SystemExit(main())
