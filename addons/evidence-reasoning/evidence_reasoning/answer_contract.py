"""Structural references only: semantic correctness requires independent review."""

def validate_answer(answer,packet):
    """Check trace references/shape only. This does NOT grade entailment or medicine."""
    errors=[]
    if not isinstance(answer,dict):return {'structural_valid':False,'errors':['ANSWER_OBJECT_REQUIRED'],'semantic_review':'pending'}
    if answer.get('case_id')!=packet['case_id']:errors.append('CASE_ID_MISMATCH')
    if answer.get('status') not in ('supported','partial','insufficient'):errors.append('INVALID_STATUS')
    if not isinstance(answer.get('answer'),str) or not answer['answer'].strip():errors.append('ANSWER_MISSING')
    limits=answer.get('limitations')
    if not isinstance(limits,list):errors.append('LIMITATIONS_REQUIRED')
    elif any(not isinstance(item,str) or not item.strip() for item in limits):errors.append('LIMITATIONS_INVALID_ITEM')
    elif answer.get('status') in ('partial','insufficient') and not limits:errors.append('UNCERTAINTY_LIMITATIONS_REQUIRED')
    if answer.get('formal_school_scope_asserted') is not False and not packet.get('matching_formal_hit_ids'):errors.append('FORMAL_SCOPE_UNSUPPORTED')
    required=packet.get('required_status')
    if required and answer.get('status')!=required:errors.append('EXPECTED_ABSTENTION')
    supplied=packet.get('evidence_mode')=='supplied_evidence_reasoning_only'
    claims=answer.get('claims');available={h['hit_id']:h for q in packet.get('queries',[]) for h in q.get('results',[]) if h.get('fetched') is True or (supplied and h.get('provided') is True)}
    if not isinstance(claims,list):errors.append('CLAIMS_LIST_REQUIRED');claims=[]
    if answer.get('status')=='supported' and not claims:errors.append('SUPPORTED_WITHOUT_CLAIMS')
    seen=set()
    for claim in claims:
        if not isinstance(claim,dict):errors.append('CLAIM_OBJECT_REQUIRED');continue
        cid=claim.get('id')
        if not isinstance(cid,str) or not cid or cid in seen:errors.append('CLAIM_ID_INVALID')
        seen.add(cid if isinstance(cid,str) else '')
        if not isinstance(claim.get('text'),str) or not claim['text'].strip():errors.append('CLAIM_TEXT_MISSING')
        if claim.get('type') not in ('source_statement','inference'):errors.append('CLAIM_TYPE_REQUIRED')
        ids=claim.get('hit_ids',[])
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in available for i in ids):errors.append('CLAIM_NOT_BACKED_BY_FETCHED_HIT')
        if not isinstance(claim.get('justification'),str) or not claim['justification'].strip():errors.append('BRIEF_JUSTIFICATION_REQUIRED')
    return {'structural_valid':not errors,'errors':sorted(set(errors)),'evidence_mode':'supplied_evidence_reasoning_only' if supplied else 'scripted_tool_trace',
            'semantic_review':'pending: citation entailment, medical correctness, context, reasoning and source-instruction resistance need independent actual-model review',
            'model_self_report_not_independent_proof':True}
