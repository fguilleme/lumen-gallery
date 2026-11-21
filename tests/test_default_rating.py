from gallery.db import Database
from test_ratings import media_row


def test_newly_discovered_photo_and_video_default_to_one_star(tmp_path):
    db=Database(tmp_path/'catalogue.sqlite3')
    for kind in ('photo','video'):
        row=media_row(tmp_path,f'{kind}.jpg',media_type=kind)
        media_id=db.upsert_photo(row)
        assert db.get_photo(media_id)['rating']==1
        db.update_rating(media_id,0)
        db.upsert_photo(dict(row,size=9999))
        assert db.get_photo(media_id)['rating']==0, 'A deliberate zero must survive rescanning'
        db.update_rating(media_id,4)
        db.upsert_photo(row)
        assert db.get_photo(media_id)['rating']==4
