"""Explicit per-excerpt user confirmations for the existing loopback Study Desk.

Never called by searches, model generation, old-log import, or daily distillation.
The browser's explicit button is a user statement, not a mastery assessment.
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import re
from .core import Ledger,InvalidEvent,EventConflict,normalized_event
from . import __version__

_lookup=None
_root=None

def bind(search_fn,root):
    global _lookup,_root
    _lookup=search_fn;_root=Path(root).resolve()

def _event(body):
    allowed={'event_id','occurred_at','action','confirmation','query','source_ref','dry'}
    if not isinstance(body,dict) or set(body)-allowed or body.get('action')!='browse' or body.get('confirmation')!='viewed_this_excerpt':raise InvalidEvent('An explicit per-excerpt browse confirmation is required')
    if 'dry' in body and type(body['dry']) is not bool:raise InvalidEvent('dry must be boolean')
    query=body.get('query');ref=body.get('source_ref')
    if not isinstance(query,str) or not query.strip() or len(query)>1400:raise InvalidEvent('Invalid query')
    if not isinstance(ref,dict) or set(ref)!={'file','source_sha256','content_hash'}:raise InvalidEvent('Invalid source reference')
    if any(not isinstance(ref[k],str) or not re.fullmatch(r'[a-f0-9]{64}',ref[k]) for k in ('source_sha256','content_hash')):raise InvalidEvent('Missing source hashes')
    if not isinstance(ref['file'],str) or len(ref['file'])>2048:raise InvalidEvent('Invalid source path')
    if _lookup is None or _root is None:raise RuntimeError('Learning event source is not bound')
    rows=_lookup(query,k=30)
    hit=next((r for r in rows if all(r.get(k)==ref[k] for k in ref)),None)
    if hit is None:raise LookupError('SOURCE_CHANGED_SEARCH_AGAIN')
    source=(_root/hit['file']).resolve()
    if not source.is_relative_to(_root):raise InvalidEvent('Source outside corpus')
    locator=' §'+str(hit.get('heading',''))+' L'+str(hit['start'])+'-'+str(hit['end'])
    pages=','.join(hit.get('pages') or [])
    if pages:locator+='; PDF physical '+pages
    sources=[{'source_id':'sha256:'+hit['source_sha256'],'title':source.name,'locator':locator[:512],'uri':source.as_uri(),'content_sha256':hit['source_sha256']}]
    if hit.get('original_source') and hit.get('original_source_sha256'):
        original=(_root/hit['original_source']).resolve()
        if original.is_relative_to(_root):sources.append({'source_id':'sha256:'+hit['original_source_sha256'],'title':original.name,'locator':'PDF physical '+pages if pages else locator[:512],'uri':original.as_uri(),'content_sha256':hit['original_source_sha256']})
    event={'event_id':body.get('event_id'),'occurred_at':body.get('occurred_at'),'actor':'user','action':'browse','topic':' '.join(query.split())[:120],'evidence':{'kind':'explicit_user_statement','ref':'ui:excerpt-browse:'+str(body.get('event_id',''))},'source_refs':sources,'note':'User explicitly confirmed viewing this exact excerpt; content_sha256='+hit['content_hash']+'; no mastery inferred.'}
    normalized,_=normalized_event(event)
    occurred=datetime.fromisoformat(normalized['occurred_at']);now=datetime.now(timezone.utc)
    if occurred>now+timedelta(minutes=5) or occurred<now-timedelta(days=31):raise InvalidEvent('Action time outside supported retry window')
    return event

def handle_get(path,query):
    if path!='/api/study/status':return 404,{'error':'unknown_study_endpoint'}
    return 200,{**Ledger().status(),'module_version':__version__,'capture_enabled':_lookup is not None,'explicit_confirmation_required':True}

def handle_post(path,body):
    if path!='/api/study/events':return 404,{'error':'unknown_study_endpoint'}
    try:
        event=_event(body)
        if body.get('dry'):return 200,{'status':'dry_run','event_id':event['event_id'],'recorded':False,'action':'browse'}
        result=Ledger().record(event)
        return 200,{**result,'action':'browse','mastery_assessed':False}
    except InvalidEvent:return 400,{'error':'invalid_explicit_browse_event'}
    except EventConflict:return 409,{'error':'event_id_conflict'}
    except LookupError:return 409,{'error':'source_changed_search_again'}
    except Exception:return 503,{'error':'local_learning_ledger_unavailable'}