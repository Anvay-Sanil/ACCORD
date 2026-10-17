"use strict";
const $ = id => document.getElementById(id);
const state = {csrf:null,role:null,dealId:null,data:null,documentId:null,detail:null,epoch:0,inspection:0,polling:false,config:null};
const labels = {AWAITING_BUYER:"Awaiting approval",CREATING_ORDER:"Preparing checkout",AUTHORIZING:"Authorizing",
  AUTHORIZED:"Authorized · not captured",AUTHORIZATION_PENDING:"Authorization pending",AUTHORIZATION_DENIED:"Authorization denied",
  CAPTURING:"Capture in progress",CAPTURE_PENDING:"Capture pending",CAPTURED:"Capture confirmed",CAPTURE_FAILED:"Capture failed",
  RECONCILING:"Reconciling",VOID_PENDING:"Cancellation pending",VOIDED:"Authorization voided",EXPIRED:"Authorization expired",
  REFUNDED:"Fully refunded",PARTIALLY_REFUNDED:"Partially refunded",REVERSED:"Payment reversed"};
function node(tag,text,className) {const n=document.createElement(tag);if(text!=null)n.textContent=text;if(className)n.className=className;return n;}
function notice(message,error=false){$("notice").textContent=message;$("notice").classList.toggle("error",error);$("notice").hidden=false;}
async function api(path,method="GET",body=null){
  const headers={};let payload;
  if(method!=="GET"){if(state.csrf)headers["X-CSRF-Token"]=state.csrf;if(body instanceof File){headers["Content-Type"]="application/pdf";payload=body;}else{headers["Content-Type"]="application/json";payload=JSON.stringify(body??{});}}
  const response=await fetch(path,{method,headers,body:payload,credentials:"same-origin",cache:"no-store",signal:AbortSignal.timeout(30000)});
  const data=await response.json();
  if(!response.ok){if(response.status===401&&state.csrf)signOutView();throw new Error(typeof data.detail==="string"?data.detail:`Request failed (${response.status})`);}
  return data;
}
function route(suffix,id=state.dealId){if(!id)throw new Error("Select an agreement first.");return `/api/deals/${encodeURIComponent(id)}/${suffix}`;}
async function action(fn){try{await fn();}catch(error){notice(error.message||"Request failed. Refresh to check its outcome.",true);}}
function signOutView(){state.epoch++;state.csrf=null;state.role=null;state.dealId=null;state.data=null;state.documentId=null;state.detail=null;state.polling=false;$("workspace").hidden=true;$("login-view").hidden=false;$("identity").textContent="";$("logout").hidden=true;$("paypal-area").hidden=true;}
$("login-form").addEventListener("submit",event=>{event.preventDefault();action(async()=>{
  const username=$("username").value;const result=await api("/api/login","POST",{username,password:$("password").value});
  state.epoch++;state.csrf=result.csrf_token;state.role=result.role;$("password").value="";$("identity").textContent=`${username} · ${result.role}`;
  const list=await api("/api/deals");$("deal-select").replaceChildren();for(const deal of list.deals){const option=node("option",`${deal.policy.purchase_order} · ${deal.policy.lot}`);option.value=deal.id;$("deal-select").append(option);}
  if(!list.deals.length){notice("This account has no assigned agreement.");return;}
  state.dealId=list.deals[0].id;await refresh();
  $("login-view").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("notice").hidden=true;
});});
$("logout").addEventListener("click",()=>action(async()=>{await api("/api/logout","POST");signOutView();}));
$("deal-select").addEventListener("change",()=>action(async()=>{state.epoch++;state.dealId=$("deal-select").value;state.documentId=null;state.detail=null;$("paypal-area").hidden=true;await refresh();}));
$("refresh").addEventListener("click",()=>action(refresh));
async function refresh(){
  if(!state.csrf||!state.dealId||state.polling)return;
  state.polling=true;const id=state.dealId,epoch=state.epoch;
  try{const data=await api(route("workspace",id));if(state.dealId!==id||state.epoch!==epoch)return;state.data=data;render();
    if(state.documentId){const doc=data.documents.find(d=>d.id===state.documentId);const signature=JSON.stringify([doc?.parse_state,doc?.extraction]);if(state.detail?.signature!==signature)await inspect(state.documentId);}
  }finally{state.polling=false;}
}
function render(){
  const {deal:d,decision,documents,operations,events}=state.data;const buyer=state.role==="buyer",cap=operations.find(o=>o.kind==="CAPTURE");
  $("deal-title").textContent=`${d.policy.purchase_order} / ${d.policy.lot}`;$("requirement").textContent=`Grade ${d.policy.material_grade}`;$("lot").textContent=`${d.policy.purchase_order} · ${d.policy.lot}`;
  $("consents").textContent=`Buyer ${d.buyer_approved?"approved":"pending"} · Supplier ${d.supplier_approved?"approved":"pending"}`;
  $("decision-state").textContent=decision?(decision.outcome==="PASS"?"Requirements passed":"Review required"):"Awaiting evidence";
  $("decision-reason").textContent=decision?.bundle.reasons?.join(" · ")||(decision?"Cited evidence meets the frozen requirement.":"Upload documents and evaluate the current evidence.");
  $("payment-state").textContent=labels[d.payment_state]||d.payment_state;
  $("deadline").textContent=d.capture_deadline?`Initiate capture by ${new Date(d.capture_deadline).toLocaleString()}`:"48 hours from PayPal authorization creation";
  $("payment-detail").textContent=d.capture_id?`Capture ${d.capture_id}`:(d.authorization_id?`Authorization ${d.authorization_id}`:"No authorization recorded");
  $("mandate").textContent=d.policy.mandate;const approved=buyer?d.buyer_approved:d.supplier_approved;
  $("approve").disabled=approved||!$("agree-check").checked;$("agree-check").disabled=approved;$("approval-note").textContent=approved?"Your approval is recorded.":"";
  $("buyer-checkout").hidden=!buyer;$("prepare-checkout").disabled=!d.buyer_approved||!d.supplier_approved||!!d.authorization_id||d.cancel_requested;
  $("recover-approval").hidden=!buyer||!d.order_id||!!d.authorization_id;
  if(d.authorization_id){$("checkout-status").textContent=labels[d.payment_state]||d.payment_state;$("paypal-area").hidden=true;}
  $("upload-label").firstChild.textContent=buyer?"Upload delivery receipt":"Upload material certificate";
  $("delivery-controls").hidden=!buyer;$("acknowledge").disabled=d.delivery_ack||!$("received-check").checked;
  $("acknowledge").textContent=d.delivery_ack?"Delivery acknowledged":"Acknowledge delivery";$("received-check").disabled=d.delivery_ack;
  $("supplier-actions").hidden=buyer;$("capture").disabled=!(decision?.outcome==="PASS"||cap?.first_dispatch_at);
  $("void").disabled=d.cancel_requested||!!cap?.first_dispatch_at;
  $("capture-explanation").textContent=decision?.outcome!=="PASS"&&!cap?.first_dispatch_at?"Capture is blocked until the current evidence passes.":cap?.state==="RECONCILING"?"The capture outcome is uncertain. Reconciliation reuses this operation; do not create another payment.":"A passing decision permits capture. Only a confirmed provider response establishes its outcome.";
  $("documents").replaceChildren();for(const doc of documents){
    const button=node("button",null,`document-row${doc.id===state.documentId?" selected":""}${doc.active?"":" inactive"}`);button.type="button";button.dataset.documentId=doc.id;
    button.append(node("strong",`v${doc.version} · ${doc.kind}`),node("span",`${doc.active?"Active":"Superseded"} · ${doc.parse_state.replaceAll("_"," ")}`));
    const finding=doc.extraction?.proposal?.findings?.[0];if(finding)button.append(node("span",`${finding.field}: ${finding.value}`));
    button.addEventListener("click",()=>action(()=>inspect(doc.id)));$("documents").append(button);
  }
  $("operations").replaceChildren();for(const op of operations){const row=node("div",null,"operation"),detail=node("div");row.append(node("strong",op.kind),detail);detail.append(node("div",`${op.state} · ${op.provider_id||"provider reference pending"}`),node("code",`Operation ${op.id}`));if(op.decision_id)detail.append(node("div",`Decision ${op.decision_id}`));if(op.last_error)detail.append(node("div",op.last_error));$("operations").append(row);}
  $("audit").replaceChildren();for(const event of events){$("audit").append(node("li",`${new Date(event.created_at).toLocaleString()} · ${event.event}\n${JSON.stringify(event.data)}`));}
  updateReviewControls();
}
async function inspect(id){
  const dealId=state.dealId,epoch=state.epoch,inspection=++state.inspection;
  if(state.documentId!==id){
    state.documentId=id;state.detail=null;$("review-reason").value="";$("replacement-check").checked=false;
    $("document-title").textContent="Loading document…";$("document-meta").textContent="";$("finding-summary").replaceChildren();
    $("passage").textContent="Loading document passages…";$("versions").textContent="";$("download").hidden=true;render();
  }
  const detail=await api(route(`documents/${id}/findings`));
  if(epoch!==state.epoch||dealId!==state.dealId||inspection!==state.inspection||id!==state.documentId)return;
  state.documentId=id;const doc=state.data.documents.find(d=>d.id===id);state.detail={...detail,signature:JSON.stringify([doc.parse_state,doc.extraction])};
  $("document-title").textContent=`v${doc.version} · ${doc.kind}`;$("document-meta").textContent=`${doc.parse_state} · SHA-256 ${doc.sha256}`;
  const findings=detail.extraction?.proposal?.findings||[];$("finding-summary").replaceChildren();for(const f of findings){$("finding-summary").append(node("div",`${f.field}: ${f.value} · ${f.purchase_order} · ${f.lot}`,"finding"));}
  $("passage").textContent=(detail.parsed?.spans||[]).map(s=>`[${s.id}] ${s.text}`).join("\n")||"Parsing or extraction is pending. A parse failure requires a usable replacement PDF.";
  $("versions").textContent=JSON.stringify(detail.extraction?.versions||{},null,2);$("download").href=route(`documents/${id}`);$("download").hidden=false;
  $("review-spans").replaceChildren();for(const s of detail.parsed?.spans||[]){const option=node("option",`[${s.id}] ${s.text}`);option.value=s.id;$("review-spans").append(option);}
  $("review-value").value=findings[0]?.value||"";$("replacement-check").checked=false;render();
}
function updateReviewControls(){
  const doc=state.data.documents.find(d=>d.id===state.documentId);$("review-controls").hidden=state.role!=="buyer"||!doc?.active||!state.detail?.parsed;
  const previous=$("replace-old").value;$("replace-old").replaceChildren();
  for(const old of state.data.documents.filter(d=>d.active&&d.kind===doc?.kind&&d.version<doc.version)){const option=node("option",`v${old.version} · ${old.extraction?.proposal?.findings?.[0]?.value||old.kind}`);option.value=old.id;$("replace-old").append(option);}
  if([...$("replace-old").options].some(o=>o.value===previous))$("replace-old").value=previous;
  $("replace").disabled=!$("replace-old").value||!$("replacement-check").checked;
}
for(const id of ["agree-check","received-check","replacement-check"])$(id).addEventListener("change",()=>{if(state.data)render();});
$("approve").addEventListener("click",()=>action(async()=>{await api(route("approve"),"POST",{policy_hash:state.data.deal.policy_hash});notice("Agreement approval recorded.");await refresh();}));
async function evaluate(){const result=await api(route("evaluate"),"POST");notice(result.outcome==="PASS"?"Requirements passed. Check the payment record for capture status.":`Capture blocked: ${result.reasons.join(" · ")}`,result.outcome!=="PASS");await refresh();}
$("evaluate").addEventListener("click",()=>action(evaluate));
$("upload-form").addEventListener("submit",event=>{event.preventDefault();action(async()=>{const file=$("document-file").files[0];if(!file||file.size>5242880)throw new Error("Choose a PDF up to 5 MB.");await api(route(`documents?kind=${state.role==="buyer"?"receipt":"certificate"}`),"POST",file);$("document-file").value="";notice("Document uploaded. Extraction is queued.");await refresh();});});
$("acknowledge").addEventListener("click",()=>action(async()=>{await api(route("delivery-ack"),"POST");await evaluate();}));
$("review-form").addEventListener("submit",event=>{event.preventDefault();action(async()=>{const doc=state.data.documents.find(d=>d.id===state.documentId),selected=[...$("review-spans").selectedOptions].map(o=>o.value),spans=state.detail.parsed.spans.filter(s=>selected.includes(s.id));
  await api(route("review"),"POST",{finding:{document_id:doc.id,document_version:doc.version,field:doc.kind==="certificate"?"delivered_grade":"delivery_received",purchase_order:state.data.deal.policy.purchase_order,lot:state.data.deal.policy.lot,value:$("review-value").value.trim(),span_ids:spans.map(s=>s.id),quote:spans.map(s=>s.text).join("\n")},reason:$("review-reason").value});await evaluate();});});
$("replace").addEventListener("click",()=>action(async()=>{if(!$("replacement-check").checked)throw new Error("Review the replacement first.");await api(route("supersede"),"POST",{old_id:$("replace-old").value,new_id:state.documentId});await evaluate();}));
$("capture").addEventListener("click",()=>action(async()=>{await api(route("capture"),"POST");notice("The existing capture operation will be used.");await refresh();}));
$("void").addEventListener("click",()=>action(async()=>{await api(route("void"),"POST");notice("Cancellation requested. Check the recorded provider outcome.");await refresh();}));
let sdkPromise;
async function sdk(){
  if(!state.config.checkout_configured)throw new Error("Sandbox merchant credentials are not configured on the server.");
  if(!sdkPromise)sdkPromise=(async()=>{await new Promise((resolve,reject)=>{const script=document.createElement("script");script.src="https://www.sandbox.paypal.com/web-sdk/v6/core";script.onload=resolve;script.onerror=()=>reject(new Error("PayPal SDK could not load."));document.head.append(script);});return window.paypal.createInstance({clientId:state.config.client_id,components:["paypal-payments"],pageType:"checkout"});})().catch(error=>{sdkPromise=null;throw error;});
  return sdkPromise;
}
$("prepare-checkout").addEventListener("click",()=>action(async()=>{
  const epoch=state.epoch,id=state.dealId;$("prepare-checkout").disabled=true;$("checkout-status").textContent="Preparing your existing PayPal order…";
  await api(route("orders",id),"POST");let orderId;
  for(let n=0;n<40;n++){if(epoch!==state.epoch)throw new Error("Session changed; reopen checkout.");const result=await api(`/api/deals/${id}`);if(result.deal.order_id){orderId=result.deal.order_id;break;}if(result.operations.some(o=>o.state==="MANUAL_REVIEW"))throw new Error("Order preparation needs review.");await new Promise(resolve=>setTimeout(resolve,1500));}
  if(!orderId)throw new Error("Order outcome is still reconciling. Refresh before retrying.");
  const instance=await sdk(),eligibility=await instance.findEligibleMethods({currencyCode:"USD",amount:"750.00"});if(!eligibility.isEligible("paypal"))throw new Error("PayPal is not eligible for this Sandbox buyer.");
  const session=instance.createPayPalOneTimePaymentSession({orderId,commit:false,onApprove:async data=>{
    if(state.epoch!==epoch||state.dealId!==id||data.orderId!==orderId)throw new Error("Checkout context changed; no authorization was requested.");
    await api(route("authorize",id),"POST");$("checkout-status").textContent="Buyer approval returned. Authorization is being reconciled.";await refresh();
  },onCancel:()=>{$("checkout-status").textContent="PayPal approval cancelled. No authorization requested.";},onError:()=>{notice("PayPal approval failed. Refresh to check the existing order before retrying.",true);}});
  $("paypal-button").onclick=()=>action(()=>session.start({presentationMode:"popup"},Promise.resolve({orderId})));
  $("paypal-area").hidden=false;$("checkout-status").textContent="Continue in PayPal to approve the $750.00 authorization.";await refresh();
}));
$("recover-approval").addEventListener("click",()=>action(async()=>{await api(route("authorize"),"POST");notice("The backend will read the existing order and verify buyer approval.");await refresh();}));
setInterval(()=>{if(state.csrf&&!document.hidden)action(refresh);},3000);
action(async()=>{state.config=await api("/api/browser-config");$("environment").textContent=state.config.label;});
