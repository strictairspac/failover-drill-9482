def get_state(client, headers):
    response = client.get('/sandbox', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_state_requires_auth(client):
    assert client.get('/sandbox').status_code == 401


def test_viewer_cannot_write(client, viewer, operator):
    assert client.post('/sandbox/orders', json={'order_no': 'HS-9001'}, headers=viewer).status_code == 403
    assert client.post('/sandbox/builds', json={'attempt_key': 'k1'}, headers=viewer).status_code == 403
    assert client.post('/sandbox/reset', json={}, headers=viewer).status_code == 403


def test_order_write_duplicate_and_correction_flow(client, operator, admin):
    created = client.post('/sandbox/orders', json={'order_no': 'HS-9001'}, headers=operator)
    assert created.status_code == 201, created.text
    duplicated = client.post('/sandbox/orders', json={'order_no': 'HS-9001'}, headers=operator)
    assert duplicated.status_code == 409
    assert '已存在' in duplicated.json()['detail']
    state = get_state(client, operator)
    dup = [o for o in state['orders'] if o['order_no'] == 'HS-2611'][-1]
    assert dup['can_correct'] is True
    corrected = client.post(f"/sandbox/orders/{dup['id']}/correct",
                            json={'order_no': 'HS-2611-R'}, headers=operator)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()['order_no'] == 'HS-2611-R'
    normal = client.post('/sandbox/orders/1/correct', json={'order_no': 'HS-0000'}, headers=operator)
    assert normal.status_code == 409


def test_build_and_failover_gate_over_http(client, operator):
    started = client.post('/sandbox/builds', json={'attempt_key': 'http-build'}, headers=operator)
    assert started.status_code == 201, started.text
    build_id = started.json()['id']
    deferred = client.post('/sandbox/failovers',
                           json={'attempt_key': 'http-fo', 'target': 'replica-east'}, headers=operator)
    assert deferred.status_code == 201
    assert deferred.json()['status'] == 'deferred'
    assert '暂缓' in deferred.json()['wait_reason']

    status = 'building'
    for _ in range(30):
        tick = client.post(f'/sandbox/builds/{build_id}/tick', headers=operator)
        if tick.status_code != 200:
            break
        status = tick.json()['status']
        if status in ('paused', 'active'):
            break
    assert status == 'paused'
    state = get_state(client, operator)
    assert state['latest_build']['stage'] == 'awaiting_correction'
    assert state['replica']['can_detect_duplicates'] is False

    # 旧重单纠正后构建继续，直到索引生效；暂缓的切主被放行。
    for _ in range(6):
        state = get_state(client, operator)
        build = state['latest_build']
        if build['status'] == 'active':
            break
        if build['stage'] == 'awaiting_correction' and build['blocking_order_id']:
            client.post(f"/sandbox/orders/{build['blocking_order_id']}/correct",
                        json={'order_no': f"R-{build['blocking_order_id']}"}, headers=operator)
        for _ in range(10):
            tick = client.post(f'/sandbox/builds/{build_id}/tick', headers=operator)
            if tick.status_code != 200 or tick.json()['status'] in ('paused', 'active'):
                break
    state = get_state(client, operator)
    assert state['latest_build']['status'] == 'active'
    assert state['replica']['can_detect_duplicates'] is True
    fo = state['failovers']
    assert fo[0]['status'] == 'completed'
    effective = client.post('/sandbox/failovers',
                            json={'attempt_key': 'http-fo-2', 'target': 'replica-west'}, headers=operator)
    assert effective.json()['status'] == 'completed'


def test_tick_steps_batch_and_unknown_build(client, operator):
    started = client.post('/sandbox/builds', json={'attempt_key': 'batch'}, headers=operator).json()
    tick = client.post(f"/sandbox/builds/{started['id']}/tick", json={'steps': 20}, headers=operator)
    assert tick.status_code == 200
    assert tick.json()['status'] == 'paused'
    assert tick.json()['resume_stage'] == 'scanning'
    assert client.post('/sandbox/builds/999/tick', json={}, headers=operator).status_code == 404


def test_replica_check_reports_coverage(client, viewer, operator):
    uncovered = client.post('/sandbox/replica-check', json={'order_no': 'HS-2601'}, headers=viewer)
    assert uncovered.status_code == 200
    body = uncovered.json()
    assert body['can_detect_duplicates'] is False
    assert body['would_be_rejected_on_replica'] is False
    assert '副本' in body['reason']
