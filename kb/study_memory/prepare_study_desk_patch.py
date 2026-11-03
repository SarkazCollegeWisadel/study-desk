"""Prepare exact Study Desk source patches; output stays local until deployment."""
from pathlib import Path
import argparse,hashlib,json

def one(text,old,new):
    if text.count(old)!=1:raise ValueError('Patch anchor is missing or ambiguous')
    return text.replace(old,new,1)

def server_patch(text):
    text=one(text,'    print("正在加载检索索引（优先 CPU 全文索引）...", flush=True)', '''    try:
        from study_memory import http_api as study_api
        study_err = ''
    except Exception:
        study_api, study_err = None, 'Learning event module unavailable'

    print("正在加载检索索引（优先 CPU 全文索引）...", flush=True)''')
    text=one(text,'    html_path = KB_DIR / "web" / "index.html"', '''    if study_api is not None:
        study_api.bind(lambda q, k=30: r.search(q, k=k), cfg['root'])
    html_path = KB_DIR / "web" / "index.html"''')
    text=one(text,'            elif self.path.startswith("/api/unit/"):', '''            elif self.path.startswith('/api/study/'):
                self._mod(study_api, study_err, 'GET', None)
            elif self.path.startswith("/api/unit/"):''')
    text=one(text,'            if self.path.startswith("/api/unit/"):', '''            if self.path.startswith('/api/study/'):
                self._mod(study_api, study_err, 'POST', req)
                return
            if self.path.startswith("/api/unit/"):''')
    compile(text,'build_kb.py','exec')
    return text

CLIENT=r'''
function bindStudyBrowse(box,results,query){
  box.querySelectorAll('[data-study-browse]').forEach(button=>{
    const hit=results[+button.dataset.studyBrowse];
    button.onclick=async event=>{
      if(!event.isTrusted)return;
      if(new URLSearchParams(location.search).get('dry')==='1'||location.hash==='#dry'){
        toast('试运行不记录查阅。');return;
      }
      if(!hit.source_sha256||!hit.content_hash){toast('此片段缺少来源校验，请重新检索。');return;}
      const pendingKey='kb.study.pending.'+hit.content_hash+':'+encodeURIComponent(query);
      let record;
      try{
        record=JSON.parse(localStorage.getItem(pendingKey)||'null');
        if(!record){
          record={event_id:'browse:'+crypto.randomUUID(),occurred_at:new Date().toISOString(),
            action:'browse',confirmation:'viewed_this_excerpt',query,
            source_ref:{file:hit.file,source_sha256:hit.source_sha256,content_hash:hit.content_hash}};
          localStorage.setItem(pendingKey,JSON.stringify(record));
        }
      }catch(_){toast('无法保存重试标识，本次未提交记录。');return;}
      button.disabled=true;
      try{
        const result=await api('/api/study/events',record);
        if(result.error){
          if(result.error==='source_changed_search_again'){
            localStorage.removeItem(pendingKey);button.textContent='来源已更新，请重新检索';return;
          }
          throw Error(result.error);
        }
        localStorage.removeItem(pendingKey);button.textContent='已记录这次查阅';
        toast('已记下本次查阅；每日汇总，不评估掌握程度。');
      }catch(_){button.disabled=false;button.textContent='记为已查阅（重试）';toast('记录未确认，可重试；不会重复计数。');}
    };
  });
}
'''

def html_patch(text):
    text=one(text,'function hitHTML(it,i,acts){',CLIENT+'\nfunction hitHTML(it,i,acts){')
    text=one(text,'    <div class="body">${esc(it.text)}</div>', '''    <div class="body">${esc(it.text)}</div>
    <div class="acts"><button class="b sm" data-study-browse="${i}"><span>记为已查阅</span></button><small>只记录这条资料的查阅，不表示掌握</small></div>''')
    text=one(text,"    box.innerHTML=h||'<div class=\"empty\">无结果</div>';", "    box.innerHTML=h||'<div class=\"empty\">无结果</div>';\n    bindStudyBrowse(box,d.results,q);")
    return text

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kb-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    entries=[]
    for rel,patch in [('build_kb.py',server_patch),('web/index.html',html_patch)]:
        source=a.kb_dir/rel;raw=source.read_bytes();modified=patch(source.read_text(encoding='utf-8'));target=a.output/source.name;target.write_text(modified,encoding='utf-8')
        entries.append({'source_relative':rel,'before_sha256':hashlib.sha256(raw).hexdigest(),'prepared_file':target.name,'after_sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    (a.output/'patches.json').write_text(json.dumps(entries,indent=2),encoding='utf-8');print(json.dumps({'prepared_files':len(entries),'deployed':False}))
if __name__=='__main__':main()