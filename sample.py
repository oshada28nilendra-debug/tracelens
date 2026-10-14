"""Original synthetic checkout trace, not a recorded production workload."""
import json
from pathlib import Path

def fixture():
    origin = 1791936000000000000
    def span(i, parent, name, start, end, error=False):
        return {'traceId':'a'*32,'spanId':f'{i:016x}','parentSpanId':f'{parent:016x}' if parent else '',
                'name':name,'startTimeUnixNano':str(origin+start*1000000),
                'endTimeUnixNano':str(origin+end*1000000),'status':{'code':2 if error else 1}}
    def resource(name, spans):
        return {'resource':{'attributes':[{'key':'service.name','value':{'stringValue':name}}]},'scopeSpans':[{'scope':{'name':'synthetic-fixture'},'spans':spans}]}
    return {'resourceSpans':[
        resource('gateway',[span(1,0,'POST /checkout',0,500)]),
        resource('orders',[span(2,1,'checkout',20,480)]),
        resource('database',[span(3,2,'SELECT inventory',40,180),span(4,2,'SELECT inventory',100,220)]),
        resource('payments',[span(5,2,'charge',240,450,True),span(6,99,'orphan callback',510,540)])]}

if __name__ == '__main__':
    Path('examples').mkdir(exist_ok=True)
    Path('examples/checkout.json').write_text(json.dumps(fixture(),indent=2)+'\n')
