from pathlib import Path
from fastapi.testclient import TestClient
from app import Settings, create_app

PROJECT = Path(__file__).resolve().parents[1]


def client_for(tmp_path, auth=False):
    return TestClient(create_app(Settings(project_dir=PROJECT, photo_root=tmp_path/'source', data_dir=tmp_path/'data', upload_dir=tmp_path/'uploads', access_password='test', session_secret='test-secret', auth_required=auth)))


def test_similarity_status_is_explicitly_unanalyzed_initially(tmp_path):
    client = client_for(tmp_path)
    response = client.get('/api/similarity/status')
    assert response.status_code == 200
    assert response.json()['running'] is False
    assert response.json()['analyzed'] == 0


def test_similarity_requires_auth_and_rejects_cross_origin_mutations(tmp_path, monkeypatch):
    protected = client_for(tmp_path/'auth', auth=True)
    for method, path in [('get','/api/similarity/status'),('post','/api/similarity/analyze'),('post','/api/similarity/cancel')]:
        assert getattr(protected, method)(path).status_code == 401
    client = client_for(tmp_path/'public')
    manager = client.app.state.lumen_similarity
    started = []
    monkeypatch.setattr(manager, 'start', lambda: started.append(True) or True)
    for path in ['/api/similarity/analyze', '/api/similarity/cancel']:
        assert client.post(path, headers={'Origin':'https://attacker.invalid'}).status_code == 403
    assert started == []
    response = client.post('/api/similarity/analyze', headers={'Origin':'http://testserver'})
    assert response.status_code == 200
    assert started == [True]
    assert client.post('/api/similarity/cancel').status_code == 200


def test_similarity_mode_validated_before_listing(tmp_path):
    client = client_for(tmp_path)
    assert client.get('/api/photos?similarity=bogus').status_code == 422
    for mode in ['near','content']:
        response = client.get('/api/photos', params={'similarity':mode})
        assert response.status_code == 200
        assert response.json() == {'items':[], 'total':0}


def test_grouping_happens_after_filters_before_pagination_and_preserves_fields(tmp_path, monkeypatch):
    client = client_for(tmp_path)
    db = client.app.state.lumen_database
    with db.connect() as connection:
        for i in range(4):
            connection.execute('INSERT INTO photos(path,root_path,relative_path,name,format,date,size,mtime_ns,rating) VALUES(?,?,?,?,?,?,?,?,?)', (str(tmp_path/f'{i}.jpg'),str(tmp_path),f'{i}.jpg',f'{i}.jpg','JPEG','2024-01-01',100,1,i%2))
    called = []
    def arrange(rows, mode):
        called.append(([row['name'] for row in rows],mode))
        return [dict(row, similarity_group='near:2',similarity_size=2) for row in reversed(rows)]
    monkeypatch.setattr(client.app.state.lumen_similarity, 'annotate_and_sort', arrange)
    result = client.get('/api/photos', params={'similarity':'near','rating':1,'sort':'name_asc','offset':1,'limit':1}).json()
    assert called == [(['1.jpg','3.jpg'],'near')]
    assert result['total'] == 2
    assert len(result['items']) == 1
    assert result['items'][0]['name'] == '1.jpg'
    assert result['items'][0]['similarity_group'] == 'near:2'
    assert result['items'][0]['similarity_size'] == 2
    assert result['items'][0]['rating'] == 1
    client.get('/api/photos?limit=1')
    assert len(called) == 1
