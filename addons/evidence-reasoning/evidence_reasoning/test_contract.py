"""Synthetic checks for fabricated references and false acceptance; no LLM/API."""
import copy,unittest
from evidence_reasoning.answer_contract import validate_answer

class AnswerContractTests(unittest.TestCase):
    def setUp(self):
        self.packet={'case_id':'example','queries':[{'results':[{'hit_id':'real','fetched':True},{'hit_id':'not_fetched','fetched':False}]}]}
        self.answer={'case_id':'example','status':'supported','answer':'合成结论','claims':[{'id':'c1','text':'合成事实','type':'source_statement','hit_ids':['real'],'justification':'合成对应依据'}],'limitations':[],'formal_school_scope_asserted':False}
    def test_valid_structure_still_does_not_grade_medicine(self):
        report=validate_answer(self.answer,self.packet)
        self.assertTrue(report['structural_valid']);self.assertIn('pending',report['semantic_review'])
    def test_invented_or_unfetched_hit_rejected(self):
        for identifier in ['invented','not_fetched']:
            a=copy.deepcopy(self.answer);a['claims'][0]['hit_ids']=[identifier]
            self.assertIn('CLAIM_NOT_BACKED_BY_FETCHED_HIT',validate_answer(a,self.packet)['errors'])
    def test_absence_requires_abstention(self):
        self.packet['required_status']='insufficient'
        self.assertIn('EXPECTED_ABSTENTION',validate_answer(self.answer,self.packet)['errors'])
    def test_school_scope_cannot_be_invented(self):
        self.answer['formal_school_scope_asserted']=True
        self.assertIn('FORMAL_SCOPE_UNSUPPORTED',validate_answer(self.answer,self.packet)['errors'])
    def test_empty_claims_do_not_count_as_supported(self):
        self.answer['claims']=[]
        self.assertIn('SUPPORTED_WITHOUT_CLAIMS',validate_answer(self.answer,self.packet)['errors'])
    def test_inference_needs_explicit_type_and_brief_justification(self):
        self.answer['claims'][0].pop('type');self.answer['claims'][0]['justification']=''
        errors=validate_answer(self.answer,self.packet)['errors']
        self.assertIn('CLAIM_TYPE_REQUIRED',errors);self.assertIn('BRIEF_JUSTIFICATION_REQUIRED',errors)
    def test_supplied_evidence_is_labelled_and_not_tool_acceptance(self):
        self.packet['evidence_mode']='supplied_evidence_reasoning_only'
        self.packet['queries'][0]['results'][1]['provided']=True
        self.answer['claims'][0]['hit_ids']=['not_fetched']
        report=validate_answer(self.answer,self.packet)
        self.assertTrue(report['structural_valid']);self.assertEqual(report['evidence_mode'],'supplied_evidence_reasoning_only')
    def test_one_identifier_does_not_cover_both_query_conditions(self):
        self.packet['required_status']='partial'
        self.assertIn('EXPECTED_ABSTENTION',validate_answer(self.answer,self.packet)['errors'])
        self.answer['status']='partial';self.answer['limitations']=['只提供了标识A，标识B缺证据']
        self.assertTrue(validate_answer(self.answer,self.packet)['structural_valid'])
    def test_uncertain_status_requires_a_nonempty_limit_list(self):
        for status in ('partial','insufficient'):
            a=copy.deepcopy(self.answer);a['status']=status;a['limitations']=[]
            self.assertIn('UNCERTAINTY_LIMITATIONS_REQUIRED',validate_answer(a,self.packet)['errors'])
    def test_blank_or_nonstring_limit_items_are_rejected(self):
        for status in ('partial','insufficient'):
            for limits in ([''],[' '],[None],[1],[True],['known',None]):
                a=copy.deepcopy(self.answer);a['status']=status;a['limitations']=limits
                self.assertIn('LIMITATIONS_INVALID_ITEM',validate_answer(a,self.packet)['errors'])
    def test_truthy_fetched_values_are_not_execution_evidence(self):
        for value in ('false','true',1,{},['x']):
            packet=copy.deepcopy(self.packet);packet['queries'][0]['results'][0]['fetched']=value
            self.assertIn('CLAIM_NOT_BACKED_BY_FETCHED_HIT',validate_answer(self.answer,packet)['errors'])
if __name__=='__main__':unittest.main()
