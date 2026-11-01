from datetime import date
import json

import h3
import pytest
from shapely.geometry import Point,Polygon

from app.jobs.health_osm import (
    SOURCE,classification,inactive,inherit_building_addresses,prepare,read_features,
    same_institution,to_record,
)
from app.jobs.health_import import build_plan
from app.jobs.health_pipeline import validate_reviewed

TODAY=date(2026,11,2)
GRID={h3.latlng_to_cell(49.84,24.03,9)}


def feature(name='Private Dental',**tags):
    return dict(kind='node',id=1,tags={'name':name,'amenity':'dentist',**tags},
                geometry={'type':'Point','coordinates':[24.03,49.84]},updated_at='2026-10-01T00:00:00Z')


@pytest.mark.parametrize('tags,expected',[
    ({'amenity':'dentist'},'dentistry'),
    ({'healthcare':'dentist'},'dentistry'),
    ({'amenity':'clinic'},'polyclinic'),
    ({'healthcare':'doctor'},'polyclinic'),
    ({'amenity':'hospital'},'hospital'),
    ({'amenity':'hospital','healthcare':'clinic'},'polyclinic'),
    ({'amenity':'clinic','name':'Стоматологічна поліклініка'},'dentistry'),
    ({'amenity':'clinic','name':'Медична лабораторія'},''),
    ({'amenity':'doctors','website':'https://www.csdlab.ua/'},''),
    ({'amenity':'veterinary'},''),
    ({'healthcare':'rehabilitation'},''),
    ({'healthcare':'alternative','amenity':'clinic'},''),
])
def test_only_approved_health_services(tags,expected):
    assert classification(tags)==expected


@pytest.mark.parametrize('tags',[{'disused':'yes'},{'status':'closed'},{'opening_hours':'closed'},
                                  {'end_date':'2024-01-01'},{'end_date':'unknown'}])
def test_closed_places_are_not_imported(tags):
    assert inactive(tags,TODAY)


def test_future_end_date_is_still_active():
    assert not inactive({'end_date':'2030-01-01'},TODAY)


def test_stable_osm_identity_and_existing_type():
    row,reason=to_record(feature(),GRID,TODAY)
    assert not reason
    assert row['source_key']=='node/1'
    assert row['source']==SOURCE
    assert validate_reviewed([row])==[row]


def test_ownership_does_not_filter_private_or_public():
    rows,_=prepare([feature(**{'operator:type':'private'}),dict(feature('Municipal Dental'),id=2)],GRID,[],TODAY)
    assert len(rows)==2
    assert rows[0]['ownership']=='private'


def test_missing_names_and_generic_unlocated_identity_stay_in_audit():
    assert to_record(feature(''),GRID,TODAY)[1]=='unnamed_provider'
    assert to_record(feature('Стоматологія'),GRID,TODAY)[1]=='generic_name_without_contact_or_address'
    assert to_record(feature('Стоматологія',phone='+380321234567'),GRID,TODAY)[0]


def test_invalid_geometry_and_outside_grid_fail_closed():
    assert to_record(dict(feature(),geometry=None),GRID,TODAY)[1]=='invalid_geometry'
    assert to_record(dict(feature(),geometry={'type':'Point','coordinates':[49.84,24.03]}),GRID,TODAY)[1]=='outside_lviv_bounds'
    assert to_record(feature(),set(),TODAY)[1]=='outside_existing_hex_grid'


def test_polygon_point_lies_inside_not_in_hole():
    polygon=Polygon([(24.029,49.839),(24.031,49.839),(24.031,49.841),(24.029,49.841)],
                    [[(24.0295,49.8395),(24.0305,49.8395),(24.0305,49.8405),(24.0295,49.8405)]])
    cells={h3.latlng_to_cell(49.84,24.03,9),h3.latlng_to_cell(49.84,24.0292,9)}
    row,_=to_record(dict(feature(),kind='way',geometry=polygon.__geo_interface__),cells,TODAY)
    assert polygon.contains(Point(float(row['longitude']),float(row['latitude'])))
    assert row['source_key']=='way/1'


def test_node_and_area_duplicates_same_name_are_merged():
    row,_=to_record(feature(),GRID,TODAY)
    other=dict(row,source_key='way/2',_geometry=Polygon([(24.029,49.839),(24.031,49.839),(24.031,49.841),(24.029,49.841)]))
    assert same_institution(row,other)


def test_unrelated_providers_at_identical_coordinates_are_preserved():
    first,_=to_record(feature('Alpha Dental'),GRID,TODAY)
    second,_=to_record(feature('Beta Dental'),GRID,TODAY)
    assert not same_institution(first,second)


def test_two_nearby_branches_with_different_addresses_remain_separate():
    first,_=to_record(feature('Chain',**{'addr:city':'Львів','addr:street':'вулиця Пекарська','addr:housenumber':'8'}),GRID,TODAY)
    second,_=to_record(feature('Chain',**{'addr:city':'Львів','addr:street':'вулиця Пекарська','addr:housenumber':'10'}),GRID,TODAY)
    assert not same_institution(first,second)


def test_two_nearby_branches_need_no_city_to_keep_different_houses():
    first,_=to_record(feature('Chain',**{'addr:street':'Пекарська вулиця','addr:housenumber':'8'}),GRID,TODAY)
    second,_=to_record(feature('Chain',**{'addr:street':'Пекарська вулиця','addr:housenumber':'10'}),GRID,TODAY)
    assert not same_institution(first,second)


def test_generic_names_do_not_merge_on_coordinates_alone():
    first,_=to_record(feature('Стоматологія',phone='+380321234567'),GRID,TODAY)
    second,_=to_record(feature('Стоматологія',phone='+380329876543'),GRID,TODAY)
    assert not same_institution(first,second)


def test_generic_names_can_share_verified_contact_identity():
    first,_=to_record(feature('Стоматологія',phone='+38 (032) 123-45-67'),GRID,TODAY)
    second,_=to_record(feature('Стоматологія',phone='0321234567'),GRID,TODAY)
    assert same_institution(first,second)


def test_public_alias_overlap_review_keeps_unrelated_private_clinic():
    old=dict(source='lviv_city_council_health',external_id='old',name='Міська поліклініка',type='polyclinic',latitude=49.84,longitude=24.03)
    rows,report=prepare([feature('Амбулаторія сімейної медицини',amenity='clinic'),
                         dict(feature('Private Medical',amenity='clinic',phone='+380321234567'),id=2)],GRID,[old],TODAY)
    assert [r['name'] for r in rows]==['Private Medical']
    assert report['skipped']['municipal_identity_needs_review']==1


def test_import_plan_repeated_with_same_osm_ids_is_unchanged():
    row,_=to_record(feature(),GRID,TODAY)
    values,first=build_plan([row],GRID,{'dentistry'},[])
    again,repeat=build_plan([row],GRID,{'dentistry'},values)
    assert first['planned_inserts']==1 and not again and repeat['unchanged']==1


def test_database_planner_keeps_distinct_osm_practitioners_same_name():
    first,_=to_record(feature('Стоматологія',phone='+380321234567'),GRID,TODAY)
    second,_=to_record(dict(feature('Стоматологія',phone='+380329876543'),id=2),GRID,TODAY)
    values,report=build_plan([first,second],GRID,{'dentistry'},[])
    assert report['planned_inserts']==2
    assert len(values)==2


def test_offline_osm_reader_and_building_address_do_not_copy_tenant_name(tmp_path):
    path=tmp_path/'fixture.osm'
    path.write_text('''<osm version="0.6">
<node id="1" lat="49.8398" lon="24.0298" version="1"/>
<node id="2" lat="49.8398" lon="24.0302" version="1"/>
<node id="3" lat="49.8402" lon="24.0302" version="1"/>
<node id="4" lat="49.8402" lon="24.0298" version="1"/>
<node id="5" lat="49.84" lon="24.03" version="1"><tag k="amenity" v="dentist"/><tag k="name" v="Private Dental"/></node>
<way id="10" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="name" v="Unrelated building owner"/>
<tag k="addr:street" v="вулиця Пекарська"/><tag k="addr:housenumber" v="8"/></way>
</osm>''',encoding='utf-8')
    features=list(read_features(path))
    assert len(features)==1
    assert inherit_building_addresses(features,path)==1
    assert features[0]['tags']['name']=='Private Dental'
    assert features[0]['tags']['addr:housenumber']=='8'
    assert features[0]['geometry']['coordinates']==[24.03,49.84]
