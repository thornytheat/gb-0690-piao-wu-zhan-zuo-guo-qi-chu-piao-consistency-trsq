from flask import render_template,request
from app import store,snapshot
from app.pipeline import run
from app.config import TITLE,MODULES,STAGES,COLUMNS,SAMPLES,DEFAULTS
from app import revision


def view(stage):
    if not 1<=stage<=7: return "页面不存在",404
    run_id=request.args.get("run")
    result=store.fetch(run_id) if run_id else run()
    sample=(SAMPLES/"dirty.csv").read_text(encoding="utf-8-sig")
    data=result["stages"][stage-1]
    seats=snapshot.state(result)["seats"]
    family=store.lineage(result["root"])
    original=store.original_of(result["root"])
    current=store.fetch(family["current"])
    # The panel always compares the original receipt with the lineage's current
    # version, whichever version the operator is browsing.
    diff=revision.compare(original,current) if len(family["versions"])>1 else None
    return render_template(f"m{stage}.html",title=TITLE,modules=MODULES,stage=stage,result=result,data=data,
                           sample=sample,defaults=DEFAULTS,columns=COLUMNS,history=store.history(),seats=seats,
                           root=result["root"],family=family,original=original,current=current,diff=diff)
