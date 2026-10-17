"""Provider double, not evidence of a real Sandbox transaction."""
import copy
import json

from accord.paypal import ProviderError


class FakePayPal:
    def __init__(self,deal,clock,namespace="1"):
        self.deal,self.clock = deal,clock
        self.calls,self.cache = [],{}
        self.order_value,self.auth,self.capture = None,None,None
        self.drop = set()
        self.hide_order_capture = False
        self.verify_result = True
        self.capture_status = "COMPLETED"
        self.before_post = None
        self.order_id,self.auth_id,self.capture_id = "ORDER"+namespace,"AUTH"+namespace,"CAPTURE"+namespace

    async def close(self):
        pass

    async def verify(self,raw,headers):
        if isinstance(self.verify_result,Exception):
            raise self.verify_result
        return self.verify_result

    def order(self):
        value = copy.deepcopy(self.order_value)
        if self.auth:
            value["purchase_units"][0]["payments"] = {"authorizations":[self.auth]}
        if self.capture and not self.hide_order_capture:
            value["purchase_units"][0].setdefault("payments",{})["captures"] = [self.capture]
        return value

    async def get(self,path):
        self.calls.append(("GET",path,None,None))
        if "/orders/" in path and self.order_value:
            return self.order()
        if "/authorizations/" in path and self.auth:
            return copy.deepcopy(self.auth)
        if "/captures/" in path and self.capture:
            return copy.deepcopy(self.capture)
        raise ProviderError(404,"RESOURCE_NOT_FOUND")

    async def post(self,path,payload,key,deadline=None):
        self.calls.append(("POST",path,payload,key))
        if self.before_post:
            await self.before_post(path)
        if key in self.cache:
            return copy.deepcopy(self.cache[key])
        body = json.loads(payload)
        if path == "/v2/checkout/orders":
            self.order_value = {"id":self.order_id,"status":"APPROVED","intent":"AUTHORIZE",
                "payer":{"payer_id":"BUYER1"},"purchase_units":body["purchase_units"]}
            result = self.order()
        elif path.endswith("/authorize"):
            self.auth = {"id":self.auth_id,"status":"CREATED","amount":{"currency_code":"USD","value":"750.00"},
                "create_time":self.clock().isoformat()}
            result = self.order()
        elif path.endswith("/capture"):
            self.capture = {"id":self.capture_id,"status":self.capture_status,"amount":body["amount"],
                "payee":{"merchant_id":"MERCHANT1"},"supplementary_data":{"related_ids":{"order_id":self.order_id,"authorization_id":self.auth_id}}}
            result = self.capture
        elif path.endswith("/void"):
            self.auth["status"] = "VOIDED"
            result = {}
        else:
            raise AssertionError(path)
        self.cache[key] = copy.deepcopy(result)
        if path in self.drop:
            self.drop.remove(path)
            raise ProviderError()
        return copy.deepcopy(result)
