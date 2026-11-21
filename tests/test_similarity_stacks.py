from pathlib import Path
from collections import Counter
from fastapi.testclient import TestClient
from app import Settings, create_app

PROJECT=Path(__file__).resolve().parents[1]


def prepared(tmp_path,monkeypatch):
    app=create_app(Settings(project_dir=PROJECT,photo_root=tmp_path/'source',data_dir=tmp_path/'data',access_password='test',session_secret='test',auth_required=False))
    with app.state.lumen_database.connect() as db:
        for i in range(1,209):
            db.execute('INSERT INTO photos(id,path,root_path,relative_path,name,format,media_type,date,size,mtime_ns,rating) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(i,str(tmp_path/f'{i}.jpg'),str(tmp_path),f'{i}.jpg',f'{i:03}.jpg','JPEG' if i<208 else 'MP4','photo' if i<208 else 'video','2025-01-01',100,1,0 if i==1 else 1))
    def annotate(rows,mode,keep_singletons=False):
        def group(row):
            return f'{mode}:1' if row['id']<=205 else (f'{mode}:206' if row['id']<=207 else None)
        sizes=Counter(group(row) for row in rows)
        return [dict(row,similarity_group=group(row) if group(row) and (keep_singletons or sizes[group(row)]>=2) else None,similarity_size=sizes[group(row)] if group(row) else 1) for row in rows]
    monkeypatch.setattr(app.state.lumen_similarity,'annotate_and_sort',annotate)
    return TestClient(app)


def test_group_members_are_filtered_before_pagination_even_past_200(tmp_path,monkeypatch):
    client=prepared(tmp_path,monkeypatch)
    result=client.get('/api/photos',params={'similarity':'near','similarity_group':'near:1','sort':'name_asc','offset':200,'limit':200}).json()
    assert result['total']==205
    assert [item['id'] for item in result['items']]==[201,202,203,204,205]
    assert all(item['similarity_group']=='near:1' for item in result['items'])
    one=client.get('/api/photos',params={'similarity':'near','similarity_group':'near:1','rating':0}).json()
    assert one['total']==1
    assert one['items'][0]['id']==1
    assert one['items'][0]['similarity_group']=='near:1'
    assert one['items'][0]['similarity_size']==1


def test_stack_options_require_matching_similarity_mode(tmp_path,monkeypatch):
    client=prepared(tmp_path,monkeypatch)
    for query in ('similarity_view=stacks','similarity_group=near:1','similarity=near&similarity_group=content:1','similarity=near&similarity_group=near:bad','similarity=near&similarity_group=near:1&similarity_view=stacks'):
        assert client.get('/api/photos?'+query).status_code==422,query
    empty=client.get('/api/photos?similarity=near&similarity_group=near:999').json()
    assert empty=={'items':[],'total':0}


def test_stack_overview_collapses_complete_groups_before_card_pagination(tmp_path,monkeypatch):
    client=prepared(tmp_path,monkeypatch)
    result=client.get('/api/photos',params={'similarity':'near','similarity_view':'stacks','sort':'name_asc','limit':1}).json()
    assert result['total']==3
    assert result['media_total']==208
    assert len(result['items'])==1
    first=result['items'][0]
    assert first['is_stack'] is True
    assert first['similarity_size']==205
    assert first['similarity_group']=='near:1'
    assert [cover['id'] for cover in first['stack_covers']]==[1,2,3]
    second=client.get('/api/photos',params={'similarity':'near','similarity_view':'stacks','sort':'name_asc','offset':1,'limit':2}).json()
    assert second['items'][0]['similarity_group']=='near:206'
    assert second['items'][0]['similarity_size']==2
    assert second['items'][1]['id']==208
    assert not second['items'][1].get('is_stack',False)
