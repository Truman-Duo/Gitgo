"""Developer observer regression; not a product semantic evaluation."""
from pathlib import Path
import copy,runpy,pytest
check=runpy.run_path(str(Path(__file__).resolve().parents[1]/'dev-artifacts/verify-pinned-instance.py'))['assert_pinned_preserved']
def record():return {'version_policy':'pinned','definition_revision':1,'epoch':1,'effective_ref':'old','phase':'ready','nodes':[],'maintenance':[]}
def test_pinned_notice_does_not_change_effective_instance():
    old=record();new=copy.deepcopy(old)
    new['maintenance']=[{'id':'notice','status':'pinned','target_ref':'new','error':None}]
    check(old,new)
@pytest.mark.parametrize('field,value',[('definition_revision',2),('epoch',2),('effective_ref','new'),('phase','blocked')])
def test_real_changes_cannot_be_hidden_by_notice(field,value):
    old=record();new=copy.deepcopy(old);new[field]=value
    with pytest.raises(AssertionError):check(old,new)
