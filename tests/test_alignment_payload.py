import json
import pytest
from backend.core.loop.alignment_payload import encode,decode


def test_repeated_evidence_roundtrips_without_changing_quotes_nulls_or_paths():
    text='Actual user requirement: 保持公开签名，不授予权限。'*500
    payload={'request':text,'quote':text,'a/b~c':{'items':[None,text]},'literal':{'$text_ref':'t0'}}
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    packed=encode(payload)
    assert len(packed.encode())<len(raw.encode())
    assert decode(packed)==payload
    assert payload['a/b~c']['items']==[None,text]


def test_small_payload_is_original_json_and_unique_evidence_is_never_dropped():
    small={'request':'Original request','actual_answers':[]}
    assert json.loads(encode(small))==small
    unique={'request':'x'*90000,'other':'y'*10000}
    assert decode(encode(unique))==unique
    assert len(encode(unique).encode())>90000


def test_source_capacity_remains_bounded_even_with_highly_repeated_text():
    with pytest.raises(ValueError,match='bounded evidence'):
        encode({'sources':['x'*10000]*30})


def test_provider_receives_decodable_evidence_and_original_quote_is_preserved():
    from backend.core.loop.alignment_runtime import assess_with_provider
    quote='Keep the public signature and original tests. '*900
    payload={'stage':'admission','original_request':quote,'request':quote,'conversation_context':[{'role':'user','content':quote}]}
    class Provider:
        max_output_tokens=4096
        def chat(self,messages,**kwargs):
            assert 'resolve text_bindings' in messages[0]['content']
            assert decode(messages[1]['content'])==payload
            return {'content':json.dumps({'status':'ready','interpretations':[],'questions':[],
                'request_frame':{'kind':'deliverable','source_quote':quote,'outcome':'Preserve the interface'}})}
    result=assess_with_provider(Provider(),payload)
    assert result['request_frame']['source_quote']==quote


def test_later_duplicate_and_lexically_earlier_text_preserve_original_prefix():
    stable='Stable original user requirements. '*1200
    a={'original_request':stable,'request':stable,'candidate':'old candidate'}
    b={**a,'candidate':['A newly repeated later source. '*300]*2}
    wa,wb=encode(a),encode(b)
    prefix=wa.split(',"candidate":',1)[0]
    assert len(prefix.encode())>30000
    assert wb.startswith(prefix+',"candidate":')
    assert json.loads(wb)['payload']['original_request']==stable
    assert decode(wa)==a and decode(wb)==b


def test_later_duplicate_keeps_earlier_unique_evidence_inline():
    stable='Stable evidence. '*2400
    tail='Independent evidence. '*1000
    a={'original_request':stable,'request':stable,'tail':tail,'candidate':'short'}
    b={**a,'candidate':tail}
    prefix=encode(a).split(',"candidate":',1)[0]
    assert encode(b).startswith(prefix+',"candidate":')
    assert decode(encode(b))==b


def test_legacy_v1_evidence_remains_decodable():
    value={'encoding':'gitgo.alignment-texts.v1','payload':{'original_request':None},
        'shared_texts':['An original source'],'text_bindings':{'/original_request':0}}
    assert decode(json.dumps(value))=={'original_request':'An original source'}


def test_shared_records_preserve_nested_references_nulls_and_independent_copies():
    record={'ids':list(range(50)),'value':'An exact short user answer','nested':[{'key':'value','unset':None}]}
    payload={'padding':'P'*81000,'first':record,'second':record,'later_nested':record['nested']}
    packed=encode(payload)
    assert json.loads(packed)['encoding']=='gitgo.alignment-values.v3'
    restored=decode(packed)
    assert restored==payload
    restored['second']['nested'][0]['key']='changed'
    assert restored['first']['nested'][0]['key']=='value'


def test_repeated_short_record_fields_fit_without_discarding_any_evidence():
    records=[{'id':str(i),'question':'Question '+str(i),'choices':[{'id':str(j),'description':'Description '+str(j)} for j in range(10)]} for i in range(100)]
    payload={'workflow':records,'questions_asked':records,'accepted_plan':records,'candidate':'New candidate'}
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    assert len(raw.encode())>120000
    wire=encode(payload)
    assert len(wire.encode())<120000 and decode(wire)==payload


def test_legacy_v2_evidence_remains_decodable():
    value={'encoding':'gitgo.alignment-texts.v2','payload':{'original':'Exact source','copy':None},
        'text_bindings':{'/copy':'/original'}}
    assert decode(json.dumps(value))=={'original':'Exact source','copy':'Exact source'}


def test_source_quote_rejects_matching_paraphrase_and_identifies_real_sources():
    from backend.core.loop.alignment_scope import validate_request_frame
    payload={'actual_answers':[{'answer':'新增独立恢复定义并固定启用。'}],
        'historical_decisions':[{'value':'改为新增独立恢复定义并固定启用。'}]}
    frame={'kind':'deliverable','outcome':'Enable recovery','source_quote':'改为新增独立恢复定义并固定启用。'}
    with pytest.raises(ValueError,match=r'actual_answers\[\].answer'):
        validate_request_frame({'request_frame':frame},payload)
    frame['source_quote']='新增独立恢复定义'
    assert validate_request_frame({'request_frame':frame},payload)==frame


def test_alignment_places_unchanged_contract_before_changing_question_frontier():
    from backend.core.loop.alignment_runtime import assess_with_provider
    messages=[]
    class Provider:
        max_output_tokens=4096
        def chat(self,wire,**kwargs):
            messages.append(wire[1]['content'])
            return {'content':json.dumps({'status':'ready','request_frame':{'kind':'deliverable'}})}
    stable='An accepted constraint with exact source evidence. '*300
    payload={'original_request':'Original goal','task_maintenance':{'task_id':'one','contract':{'criteria':stable}},
        'confirmed_answers':[],'questions_asked':[{'question':'Earlier frontier'}],'stage':'admission'}
    assess_with_provider(Provider(),payload)
    assess_with_provider(Provider(),{**payload,'confirmed_answers':[{'answer':'A new real answer'}],
        'questions_asked':[{'question':'Changed frontier'}]})
    prefix=messages[0].split(',"confirmed_answers":',1)[0]
    assert stable in prefix and messages[1].startswith(prefix+',"confirmed_answers":')
