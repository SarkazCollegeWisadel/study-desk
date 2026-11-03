"""Build a local provenance catalog from existing audits. No crawling or LLM calls.

Inputs may contain private URLs. Only hashes, root-relative locations, a query-free
URL and document metadata enter LOCAL-ONLY outputs. Do not publish generated data.
"""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import hashlib,json,os,re,shutil,urllib.request

SUBJECTS=()  # supplied by --subjects or a deployment config; empty means none

TOPICS=[]
SCOPE_TEMPLATE='{subject}/课程大纲/章节目录_非正式教学大纲.md'
ACQUIRED=''
TOPIC_SUBJECT=''

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def clean_url(value):
    p=urlsplit(str(value or ''))
    if p.scheme not in ('http','https') or not p.hostname:return None
    # Discard user info, ports, query, and fragment, including session/signature IDs.
    return urlunsplit((p.scheme,p.hostname,p.path,'',''))

def classify(name):
    if '非正式' in name:return 'chapter_scope_or_teacher_objectives_not_formal_syllabus'
    if '执' in name and '考' in name:return 'licensing_exam_reference_not_school_syllabus'
    if '教学大纲' in name:return 'teaching_syllabus'
    if '考试大纲' in name:return 'exam_syllabus'
    if '课表' in name:return 'historical_timetable_not_current_progress'
    if '课程简介' in name:return 'course_description'
    if '复习' in name or '病例对照' in name:return 'review_or_teacher_exercise'
    return 'textbook_or_study_material'

VERSION_MARKERS=()  # (filename marker, note) pairs supplied by --version-markers


def version_notes(name):
    notes=[]
    for marker,note in VERSION_MARKERS:
        if marker and marker in name:notes.append(note)
    if '2025-2026-2' in name:notes.append('旧学期课表，不能作为当前学期进度')
    return notes

def parse_scope(text,source_id):
    lines=text.splitlines();nodes=[]
    for i,line in enumerate(lines):
        m=re.match(r'^(\d+(?:\.\d+)*)\s*(.*)$',line.strip())
        if not m:continue
        label=m[2].strip();end=i+1
        if not label and i+1<len(lines):label=lines[i+1].strip();end=i+2
        if label:nodes.append({'node_id':source_id+':L'+str(i+1),'number':m[1],'title':label,'source_id':source_id,'start_line':i+1,'end_line':end,'coverage':'course_scope_only_not_answer_coverage'})
    return nodes

class Catalog:
    def __init__(self,corpus,archive,raw):
        self.roots={'corpus':Path(corpus).resolve(),'archive':Path(archive).resolve(),'download':Path(raw).resolve()}
        self.sources={};self.relations=[];self.issues=[];self.hashes={}
    def location(self,path):
        path=Path(path).resolve()
        for name,root in sorted(self.roots.items(),key=lambda x:len(str(x[1])),reverse=True):
            if path.is_relative_to(root):return {'root':name,'path':path.relative_to(root).as_posix()}
        raise ValueError('Source is outside explicitly selected roots')
    def add(self,path,subject,kind=None,expected=None,acquired=None,pages=None):
        path=Path(path).resolve();loc=self.location(path)
        if not path.is_file():self.issues.append({'kind':'missing','location':loc});return None
        if path not in self.hashes:self.hashes[path]=digest(path)
        actual=self.hashes[path];sid='sha256:'+actual
        ok=not expected or actual==expected
        if not ok:self.issues.append({'kind':'hash_mismatch','location':loc,'expected':expected,'actual':actual})
        if sid not in self.sources:
            self.sources[sid]={'source_id':sid,'sha256':actual,'title':path.name,'subjects':[],'document_type':kind or classify(path.name),'versions':version_notes(path.name),'locations':[],'integrity_verified':ok,'expected_hash_matched':None if not expected else ok,'captured_on':acquired,'captured_time_precision':'day' if acquired else 'unknown','pages':pages,'source_text_not_embedded':True}
        item=self.sources[sid]
        if subject not in item['subjects']:item['subjects'].append(subject)
        if loc not in item['locations']:item['locations'].append(loc)
        if not ok:item['integrity_verified']=False
        return sid
    def read(self,name):return json.loads((self.roots['archive']/name).read_text(encoding='utf-8-sig'))

def search(api,query):
    p=urlsplit(api)
    if p.hostname not in ('127.0.0.1','localhost','::1') or p.scheme!='http' or p.username or p.query:raise ValueError('Only an existing loopback KB API is allowed')
    request=urllib.request.Request(api.rstrip('/')+'/api/search',data=json.dumps({'query':query,'mode':'search','k':3},ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=12) as response:return json.load(response)['results']

def build(corpus,archive,raw,api):
    c=Catalog(corpus,archive,raw);sync=c.read('学科文件夹同步核验.json');assets=c.read('来源与文件核验.json');audited=c.read('原始课程资料核验.json');converted=c.read('Markdown转换核验.json')
    by_hash={}
    for entry in sync:
        if entry['subject'] not in SUBJECTS:continue
        sid=c.add(entry['destination'],entry['subject'],expected=entry['sha256'],acquired=entry.get('acquired_on') or ACQUIRED or None)
        if sid and sid=='sha256:'+entry['sha256'] and c.sources[sid]['integrity_verified']:by_hash[entry['sha256']]=sid
    for entry in audited:
        c.add(entry.get('destination') or entry['path'],entry['subject'],expected=entry['sha256'],pages=entry.get('pages'))
    for entry in assets:
        # Original downloads can use a course label; the cleaned archive is authoritative here.
        asset_path=Path(archive)/entry['file']
        sid=c.add(asset_path,entry['subject'],expected=entry['sha256'],acquired=entry.get('acquired_on') or ACQUIRED or None)
        if sid and sid=='sha256:'+entry['sha256'] and c.sources[sid]['integrity_verified']:by_hash[entry['sha256']]=sid
    for entry in converted:
        src=by_hash.get(entry['source_sha256'])
        dst=by_hash.get(entry['markdown_sha256'])
        if src and dst:
            c.relations.append({'from_source_id':dst,'relation':'transcribed_from','to_source_id':src,'method':entry['method'],'pages':entry.get('pages'),'ocr_verified_word_for_word':False if 'OCR' in entry['method'] else None})
            c.sources[dst]['transcription_method']=entry['method']
    # Explicit platform PDF <-> DOC relationship; keep distinct asset hashes.
    for entry in assets:
        if not entry.get('isPlatformConversion'):continue
        base=entry['name'].replace('_平台转换版.pdf','.doc')
        originals=[x for x in assets if x['subject']==entry['subject'] and x['name']==base and not x.get('isPlatformConversion')]
        if originals:c.relations.append({'from_source_id':by_hash[entry['sha256']],'relation':'platform_conversion_of','to_source_id':by_hash[originals[0]['sha256']]})
    courses=[]
    for subject in SUBJECTS:
        rel=SCOPE_TEMPLATE.format(subject=subject)
        scope=Path(corpus)/rel
        scope_id=c.add(scope,subject,'chapter_scope_not_formal_syllabus',acquired=ACQUIRED or None) if scope.exists() else None
        snapshots=sorted(Path(raw).glob('*'+subject+'*_章节.json'));snapshot_id=None
        if len(snapshots)>1:c.issues.append({'kind':'ambiguous_course_snapshots','subject':subject,'candidates':[x.name for x in snapshots]})
        if len(snapshots)==1:
            snap=snapshots[0];snapshot_id=c.add(snap,subject,'private_course_page_snapshot',acquired=ACQUIRED or None)
            data=json.loads(snap.read_text(encoding='utf-8-sig'));frames=data if isinstance(data,list) else [data]
            c.sources[snapshot_id]['sanitized_origin_urls']=sorted({u for x in frames if isinstance(x,dict) for u in [clean_url(x.get('url'))] if u})
            if scope_id:c.relations.append({'from_source_id':scope_id,'relation':'exported_scope_from','to_source_id':snapshot_id})
        formal=[sid for sid,x in c.sources.items() if subject in x['subjects'] and x['document_type'] in ('teaching_syllabus','exam_syllabus')]
        courses.append({'subject':subject,'school_formal_syllabus_status':'version_or_applicability_limited' if formal else 'not_acquired','formal_source_ids':formal,'chapter_scope_source_id':scope_id,'snapshot_source_id':snapshot_id,'nodes':parse_scope(scope.read_text(encoding='utf-8-sig'),scope_id) if scope_id else [],'limitation':'目录仅证明已保存课程节点，不证明本学期完整范围/考试权重'})
    topics=TOPICS  # (title, query) pairs from --topics; empty keeps the section present but unpopulated
    review=[]
    for topic in topics:
        title,query,topic_subject=topic['title'],topic['query'],topic['subject']
        citations=[]
        for row in search(api,query):
            md=Path(corpus)/row['file'];md_id=c.add(md,topic_subject,expected=row['source_sha256'])
            original_id=None
            if row.get('original_source_sha256'):original_id=c.add(Path(corpus)/row['original_source'],topic_subject,expected=row['original_source_sha256'])
            if not md_id or not c.sources[md_id]['integrity_verified'] or (original_id and not c.sources[original_id]['integrity_verified']):continue
            citations.append({'source_id':md_id,'original_source_id':original_id,'heading':row['heading'],'start_line':row['start'],'end_line':row['end'],'physical_pdf_pages':row.get('pages',[]),'evidence_status':'retrieval_link_verified_not_model_answer'})
        review.append({'topic':title,'query':query,'citations':citations,'coverage':'linked' if citations else 'missing','not_an_exam_weight_claim':True})
    catalog={'schema_version':1,'visibility':'LOCAL_ONLY_NOT_PUBLIC','observed_at':datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),'sources':list(c.sources.values()),'relations':c.relations,'issues':c.issues,'inputs':{'original_asset_records':len(audited),'new_original_and_conversion_records':len(assets),'sync_records':len(sync),'markdown_conversion_records':len(converted)},'network_requests':'loopback API search only; no OCS access or LLM calls'}
    outlines={'schema_version':1,'visibility':'LOCAL_ONLY_NOT_PUBLIC','courses':courses,'diagnostics_review':{'title':'据已存教材与课程目录整理的复习纲要','school_formal_syllabus':False,'historical_timetable_used_as_current_progress':False,'topics':review,'limitation':'未取得学校正式教学大纲与当前考试权重时明确标注；仅整理可回查的已有来源'}}
    return catalog,outlines

def write_outputs(out,catalog,outlines):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%d_%H%M%S')
    for name,data in [('source-catalog.private.json',catalog),('course-outlines.private.json',outlines)]:
        target=out/name
        if target.exists():
            saved=out/'v-2'/stamp;saved.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved/name)
        temp=target.with_suffix('.ready');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,target)
    return {'source_count':len(catalog['sources']),'relationship_count':len(catalog['relations']),'issues':len(catalog['issues']),'courses':len(outlines['courses']),'diagnostic_topics':len(outlines['diagnostics_review']['topics']),'llm_calls':0}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ['corpus-root','archive-root','raw-root','output']:p.add_argument('--'+flag,type=Path,required=True)
    p.add_argument('--kb-api',default='http://127.0.0.1:8765')
    p.add_argument('--subjects',nargs='+',required=True)
    p.add_argument('--topics',type=Path)
    p.add_argument('--scope-template',default='{subject}/课程大纲/章节目录_非正式教学大纲.md')
    p.add_argument('--acquired',default='')
    p.add_argument('--version-markers',type=Path)
    a=p.parse_args()
    global SUBJECTS,TOPICS,SCOPE_TEMPLATE,ACQUIRED,TOPIC_SUBJECT,VERSION_MARKERS
    VERSION_MARKERS=tuple(tuple(x) for x in (json.loads(a.version_markers.read_text(encoding='utf-8')) if a.version_markers else []))
    SUBJECTS=tuple(a.subjects);SCOPE_TEMPLATE=a.scope_template;ACQUIRED=a.acquired
    TOPICS=json.loads(a.topics.read_text(encoding='utf-8')) if a.topics else []
    if not isinstance(TOPICS,list) or any(not isinstance(x,dict) or
        any(not isinstance(x.get(k),str) or not x[k].strip() for k in ('title','query','subject'))
        for x in TOPICS):
        p.error('--topics must contain title/query/subject objects')
    if any(x['subject'] not in SUBJECTS for x in TOPICS):
        p.error('each topic subject must be selected by --subjects')
    catalog,outlines=build(a.corpus_root,a.archive_root,a.raw_root,a.kb_api)
    result=write_outputs(a.output,catalog,outlines)
    from render_catalog import render
    render(a.output/'复习来源目录.html',catalog,outlines,{'corpus':a.corpus_root,'archive':a.archive_root,'download':a.raw_root})
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()