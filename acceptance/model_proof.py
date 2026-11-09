"""Record real provider outputs, exact citations, and backend decisions. No fake fallback."""
import argparse
import asyncio
import json
from pathlib import Path
import httpx

from accord.config import Settings
from accord.db import digest,uid,utcnow
from accord.evidence import Finding,admissible,cited_text
from accord.extraction_worker import propose
from accord.parser import parse_pdf
from acceptance.fixtures import CASES,write_fixtures


async def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="acceptance-record/model")
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    config=Settings(database_url="postgresql://unused")
    model = config.gemini_model if config.extraction_provider == "gemini" else config.openai_model
    key = config.gemini_api_key if config.extraction_provider == "gemini" else config.openai_api_key
    report={"gate":"real_model","timestamp":utcnow().isoformat(),"provider":config.extraction_provider,"model":model,"cases":[]}
    destination=out/"model-result.json"
    if not key or key.startswith("replace-"):
        report.update(status="BLOCKED",reason="Selected model key is not configured. No model call was attempted.")
        destination.write_text(json.dumps(report,indent=2),encoding="utf-8");print(report["reason"]);return 2
    fixtures=write_fixtures(out/"fixtures")
    policy={"purchase_order":"PO-001","lot":"LOT-001","material_grade":"316L"}
    for case in CASES:
        data=(fixtures/(case["name"]+".pdf")).read_bytes()
        doc={"id":uid(),"version":1,"kind":case.get("kind","certificate"),"sha256":digest(data)}
        item={"case":case["name"],"document":doc,"expected_value":case["value"],"expected_admission":case["admit"]}
        try:
            parsed=await asyncio.to_thread(parse_pdf,data);doc["parsed"]=parsed
            extraction=await propose(config,doc,parsed)
            findings=[Finding.model_validate(f) for f in extraction["proposal"]["findings"]]
            citations=[]
            for finding in findings:
                try:cited_text(doc,finding);citations.append(True)
                except Exception:citations.append(False)
            accepted=False
            if len(findings)==1:
                f=findings[0];accepted=admissible(doc,f,policy)[0] and f.value==("YES" if doc["kind"]=="receipt" else "316L")
            value_ok=case["value"] is None or (len(findings)==1 and findings[0].value==case["value"])
            item.update(extraction=extraction,citations_valid=citations,value_accurate=value_ok,backend_admitted=accepted,
                        passed=all(citations) and value_ok and accepted==case["admit"])
        except httpx.HTTPStatusError as exc:
            error=exc.response.json().get("error",{})
            item.update(passed=False,error=type(exc).__name__,http_status=exc.response.status_code,
                        provider_error_code=error.get("code"),provider_parameter=error.get("param"),
                        provider_message=str(error.get("message",""))[:1000].replace(key,"[REDACTED]"))
            report["cases"].append(item)
            report["status"]="BLOCKED" if exc.response.status_code in (401,403,429) else "FAIL"
            destination.write_text(json.dumps(report,indent=2),encoding="utf-8")
            print(json.dumps({"status":report["status"],"http_status":exc.response.status_code,"provider_error_code":error.get("code")}))
            return 2
        except Exception as exc:
            item.update(passed=False,error=type(exc).__name__)
            if isinstance(exc,httpx.TransportError):
                # Do not burn the remaining fixture quota on a systemic HTTP failure.
                report["cases"].append(item);report["status"]="FAIL"
                destination.write_text(json.dumps(report,indent=2),encoding="utf-8")
                print(json.dumps({"status":"FAIL","error":type(exc).__name__}));return 1
        report["cases"].append(item)
        # Record each provider result immediately; an interrupted run cannot become a pass.
        report["status"]="RUNNING";destination.write_text(json.dumps(report,indent=2),encoding="utf-8")
    report["status"]="PASS" if all(x["passed"] for x in report["cases"]) else "FAIL"
    destination.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"status":report["status"],"cases":len(report["cases"]),"record":str(destination)}))
    return 0 if report["status"]=="PASS" else 1


if __name__=="__main__":raise SystemExit(asyncio.run(main()))
