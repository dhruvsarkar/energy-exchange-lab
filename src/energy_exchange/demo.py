import argparse, json
from dataclasses import asdict
from .core import Seller, Buyer, clear_market, settle

SCENARIOS = {
    'normal': dict(
        sellers=[Seller('S1',5,1,5,4), Seller('S2',4,1,5.5,3), Seller('S3',3,1,6,2)],
        buyers=[Buyer('B1',3,7,30,3), Buyer('B2',4,7.5,40,4), Buyer('B3',2,8,25,2)], cap=99),
    'cloud': dict(
        sellers=[Seller('S1',5,1,5,2), Seller('S2',4,1,5.5,3), Seller('S3',3,1,6,2)],
        buyers=[Buyer('B1',3,7,30,3), Buyer('B2',4,7.5,40,4), Buyer('B3',2,8,25,2)], cap=99),
    'utility_limit': dict(
        sellers=[Seller('S1',5,1,5,4), Seller('S2',4,1,5.5,3), Seller('S3',3,1,6,2)],
        buyers=[Buyer('B1',3,7,30,3), Buyer('B2',4,7.5,40,4), Buyer('B3',2,8,25,2)], cap=5),
    'preference_change': dict(
        sellers=[Seller('S1',5,3,6.8,2), Seller('S2',4,1,5.5,3), Seller('S3',3,1,6,2)],
        buyers=[Buyer('B1',3,7,30,3), Buyer('B2',4,7.5,40,4), Buyer('B3',2,8,25,2)], cap=99),
}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--scenario',choices=SCENARIOS,default='normal'); a=p.parse_args()
    cfg=SCENARIOS[a.scenario]
    trades=clear_market(cfg['sellers'],cfg['buyers'],cfg['cap'])
    res=settle(trades,cfg['sellers'],cfg['buyers'])
    print(json.dumps({'scenario':a.scenario,'committed_kwh':res.committed_kwh,'settled_kwh':res.settled_kwh,'released_payment':round(res.released_payment,2),'trades':[asdict(t) for t in res.trades]},indent=2))

if __name__=='__main__': main()
