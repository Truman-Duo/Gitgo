"""Developer acceptance: informational maintenance must not mask pin changes."""
def assert_pinned_preserved(before, after):
    assert before['version_policy'] == after['version_policy'] == 'pinned'
    assert {k:v for k,v in before.items() if k != 'maintenance'} == {
        k:v for k,v in after.items() if k != 'maintenance'}, 'Pinned effective instance changed'
    previous = {item['id']:item for item in before.get('maintenance', [])}
    current = {item['id']:item for item in after.get('maintenance', [])}
    assert all(current.get(key) == value for key,value in previous.items()), 'Existing maintenance changed'
    for key, value in current.items():
        if key not in previous:
            assert value['status'] == 'pinned' and value.get('error') is None
            assert value['target_ref'] != after['effective_ref'], 'New-version notice targets current pin'
