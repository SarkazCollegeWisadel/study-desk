"""Fixed offline daily entry: sync unverified legacy notes, then distill facts."""
import argparse,json
from pathlib import Path
from .core import Ledger,DEFAULT_HOME,atomic_write,canonical,now_utc
from .importer import DEFAULT_SOURCE,sync

def run(home=DEFAULT_HOME,source=DEFAULT_SOURCE):
    ledger=Ledger(home)
    try:imported=sync(ledger,source)
    except Exception as error:imported={'status':'unavailable','kind':type(error).__name__,'confirmed_learning_imported':0}
    # An offline old-log API must not block already-confirmed local user events.
    distilled=ledger.distill()
    result={'checked_at':now_utc(),'sync':imported,'distillation':distilled,'local_only':True,'llm_calls':0,'schedule_created_by_this_module':False}
    ledger.home.mkdir(parents=True,exist_ok=True)
    atomic_write(ledger.home/'daily_status.json',canonical(result)+'\n')
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir',type=Path,default=DEFAULT_HOME);p.add_argument('--source',default=DEFAULT_SOURCE);a=p.parse_args()
    try:result=run(a.data_dir,a.source)
    except Exception as error:print(json.dumps({'status':'failed','kind':type(error).__name__}));return 2
    print(json.dumps(result,ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())