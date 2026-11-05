"""Issue #34 source readers, exact geocoding, identity and dry-run regressions."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import h3
import openpyxl
import pytest

from app.jobs.health_import import build_plan, run_import
from app.jobs.health_pipeline import SOURCE, validate_reviewed, write_csv
from app.jobs.health_sources import (
    ACCREDITATION_SOURCE, enrich, exact_address_hit, identifier, load_registry,
    same_provider, split_addresses, street_house, reuse_existing, apply_type_profiles,
)
from app.jobs.load_health import categorize, prepare


def provider(**overrides):
    return dict(source=SOURCE, source_key='stable', source_row='2', name='Лікарня Святої Анни',
                city='Львів', address='Львів, вулиця Мечникова 8', sub_category='hospital',
                latitude='49.84', longitude='24.03', reviewed='yes', **overrides)


def hit(**overrides):
    value = dict(lat='49.84', lon='24.03', address={'city':'Львів', 'road':'вулиця Мечникова', 'house_number':'8'},
                 osm_type='node', osm_id=123)
    value.update(overrides)
    return value


def workbook(path, headers, rows):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()


def test_accreditation_splits_local_sites_and_preserves_identifiers(tmp_path):
    path = tmp_path/'registry.xlsx'
    workbook(path, ['Назва закладу охорони здоров’я','Ідентифікаційний код','Адреса(и) закладу охорони здоров’я'], [
        ['Міська лікарня', 1984276, 'м. Львів, вул. Мечникова, 8; вул. Пекарська, 59а, м. Київ, вул. Мечникова, 8'],
        ['', '', ''], ['Лікарня без адреси', '00123456', ''],
    ])
    rows, report = load_registry(path)
    assert len(rows) == 2
    assert {r['city'] for r in rows} == {'Львів'}
    assert len({r['source_key'] for r in rows}) == 2
    assert rows[0]['org_identifier'] == '01984276'
    assert report['invalid_source_rows'] == [4]
    assert all(r['source'] == ACCREDITATION_SOURCE for r in rows)


def test_regional_reader_with_whitespace_headers(tmp_path):
    path = tmp_path/'regional.xlsx'
    workbook(path, ['fullName ','orgIdentifier','settlement','streetType','streetName','houseNumber'],
             [['Стоматологічна поліклініка', '00123456', 'Львів', 'вулиця', 'Мечникова', 8]])
    rows, report = load_registry(path)
    assert rows[0]['sub_category'] == 'dentistry'
    assert report['total_source_rows'] == 1


def test_registry_units_at_same_address_keep_different_keys(tmp_path):
    path=tmp_path/'units.xlsx'
    workbook(path, ['fullName','orgIdentifier','settlement','streetType','streetName','houseNumber'], [
        ['Амбулаторія №1','00123456','Львів','вулиця','Мечникова',8],
        ['Амбулаторія №2','00123456','Львів','вулиця','Мечникова',8],
    ])
    rows, _=load_registry(path)
    assert len({r['source_key'] for r in rows}) == 2
    assert not same_provider(rows[0],rows[1])


def test_bad_xlsx_headers_and_corrupt_workbook_fail_closed(tmp_path):
    path = tmp_path/'bad.xlsx'
    workbook(path, ['unrelated'], [['content']])
    with pytest.raises(ValueError, match='Unknown registry'):
        load_registry(path)
    path.write_bytes(b'not an xlsx')
    with pytest.raises(Exception):
        load_registry(path)


def test_empty_workbook_is_empty(tmp_path):
    path = tmp_path/'empty.xlsx'
    workbook(path, [], [])
    assert load_registry(path)[0] == []


@pytest.mark.parametrize('value,expected', [('69-Б','69б'),('20-22','20-22'),('1/2','1/2'),('59а','59а')])
def test_house_suffixes_ranges_and_fractions(value, expected):
    assert street_house(f'Львів, вул. Пекарська, буд. {value}')[1] == expected


def test_locality_inheritance_does_not_leak_between_towns():
    sites = split_addresses('м. Львів, вул. Пекарська, 59а; вул. Руська, 20; м. Київ, вул. Руська, 12')
    assert [city for city, address in sites] == ['Львів','Львів','Київ']


def test_territorial_association_not_hospital_at_every_address(tmp_path):
    path=tmp_path/'registry.xlsx'
    workbook(path, ['Назва закладу охорони здоров’я','Ідентифікаційний код','Адреса(и) закладу охорони здоров’я'],
             [['Львівське територіальне медичне об’єднання «Клінічна лікарня»', '12345678','м. Львів, вул. Мечникова, 8']])
    assert load_registry(path)[0][0]['sub_category'] == ''


def test_clinical_categories_do_not_turn_statistics_into_poi():
    assert categorize('Стоматологічна поліклініка', 'Поліклініка') == 'dentistry'
    assert categorize('Лікувально-діагностичний центр', '') == 'polyclinic'
    assert categorize('Центр медичної статистики', '') == ''
    assert categorize('Центр реабілітації', '') == ''  # no arbitrary hospital mapping


def test_exact_geocoder_never_accepts_street_centroid_or_wrong_town():
    row = provider()
    assert exact_address_hit(row, [hit()])
    assert exact_address_hit(row, [hit(address={'city':'Львів','road':'Мечникова'})]) is None
    assert exact_address_hit(row, [hit(address={'city':'Винники','road':'Мечникова','house_number':'8'})]) is None
    assert exact_address_hit(row, [hit(address={'city':'Львів','road':'Пекарська','house_number':'8'})]) is None
    assert exact_address_hit(row, [hit(),hit(lat='49.85')]) is None
    assert exact_address_hit(row, [hit(address={'city':'Львів','road':'проспект Мечникова','house_number':'8'})]) is None


@pytest.mark.parametrize('lat,lon', [('24.03','49.84'),('nan','24'),('inf','24'),('',''),('90','24')])
def test_bad_coordinates_rejected(lat,lon):
    row=provider()
    row.update(latitude=lat,longitude=lon)
    with pytest.raises(ValueError, match='coordinates'):
        validate_reviewed([row])


def test_departments_at_one_address_are_distinct():
    a, b = provider(), provider()
    a.update(name='Амбулаторія №1',org_identifier='12345678')
    b.update(name='Амбулаторія №2',org_identifier='12345678',source_key='another')
    assert not same_provider(a,b)
    assert same_provider(a,dict(a,source=ACCREDITATION_SOURCE))


def test_missing_legacy_fields_and_exact_duplicates_reported():
    source={'name':'Лікарня','addressPostName':'Львів','addressThoroughfare':'Пекарська','addressLocatorDesignator':'8','orgType':''}
    rows, report=prepare([(2,source),(3,source),(4,{})])
    assert len(rows)==1
    assert report['exact_duplicate_rows']==[3]
    assert report['invalid_source_rows']==[4]


def test_wrong_h3_fails_and_outside_grid_excluded():
    row=provider()
    row['h3_id']=h3.latlng_to_cell(49.9,24.1,9)
    with pytest.raises(ValueError, match='H3'):
        validate_reviewed([row])
    row.pop('h3_id')
    values, report=build_plan([row],set(),{'hospital'},[])
    assert not values
    assert report['skipped']=={'outside_existing_hex_grid':1}


def test_plan_rerun_keeps_same_keys_and_no_writes():
    row=provider()
    grid={h3.latlng_to_cell(49.84,24.03,9)}
    values, report=build_plan([row],grid,{'hospital'},[])
    assert report['planned_inserts']==1
    second, rerun=build_plan([row],grid,{'hospital'},values)
    assert second==[]
    assert rerun['unchanged']==1
    assert rerun['db_writes']==0


def test_existing_foreign_provider_not_overwritten_and_units_not_dropped():
    grid={h3.latlng_to_cell(49.84,24.03,9)}
    old=dict(source='external_health',external_id='existing',name='Лікарня Святої Анни',type='hospital',
             latitude=49.84,longitude=24.03,h3_id=next(iter(grid)))
    row=provider()
    assert build_plan([row],grid,{'hospital'},[old])[0]==[]
    row['name']='Окремий підрозділ'
    assert len(build_plan([row],grid,{'hospital'},[old])[0])==1


def test_duplicate_ids_scoped_by_source_and_unmanaged_sources_rejected():
    row=provider()
    assert len(validate_reviewed([row,dict(row,source=ACCREDITATION_SOURCE)]))==2
    with pytest.raises(ValueError, match='Unmanaged'):
        validate_reviewed([dict(row,source='someone_elses_job')])


def test_missing_point_type_never_created():
    with pytest.raises(ValueError, match='configuration'):
        build_plan([provider()],set(),set(),[])


def test_offline_dry_run_does_not_create_engine(tmp_path):
    path=tmp_path/'reviewed.csv'
    write_csv(path,[provider()])
    with patch('sqlalchemy.ext.asyncio.create_async_engine',side_effect=AssertionError('database touched')):
        report=asyncio.run(run_import(path))
    assert report['db_writes']==0
    assert not report['grid_checked']


def test_write_needs_matching_target_before_connection(tmp_path, monkeypatch):
    path=tmp_path/'reviewed.csv'
    write_csv(path,[provider()])
    monkeypatch.setenv('DB_URL','postgresql://user:secret@example.invalid/health')
    with patch('sqlalchemy.ext.asyncio.create_async_engine',side_effect=AssertionError('database touched')):
        with pytest.raises(ValueError, match='fingerprint'):
            asyncio.run(run_import(path,write_db=True,expected_target='wrong'))


def test_cache_reuses_address_and_no_network_by_default(tmp_path):
    row=provider()
    cache=tmp_path/'cache.json'
    class Response:
        def raise_for_status(self): pass
        def json(self): return [hit()]
    with patch('requests.get',return_value=Response()) as request, patch('time.sleep'):
        records, report=enrich([row,dict(row,source_key='second',name='Амбулаторія')],cache,
                               live=True,limit=1,endpoint='https://configured.invalid/search',user_agent='test')
    assert request.call_count==1
    assert report['approved']==2
    with patch('requests.get',side_effect=AssertionError('network touched')):
        cached, report=enrich([row],cache)
    assert report['api_calls']==0
    assert cached[0]['reviewed']=='yes'


def test_existing_coordinates_bypass_geocoding(tmp_path):
    with patch('requests.get',side_effect=AssertionError('network touched')):
        rows, report=enrich([dict(provider(),coordinate_source='official_registry')],tmp_path/'cache.json')
    assert rows[0]['reviewed']=='yes'
    assert report['api_calls']==0


def test_cache_only_rerun_reuses_normalized_second_pass(tmp_path):
    row=provider()
    cache=tmp_path/'cache.json'
    class Response:
        def raise_for_status(self): pass
        def json(self): return [hit()]
    with patch('requests.get',return_value=Response()), patch('time.sleep'):
        first, _=enrich([row],cache,live=True,limit=1,normalize_query=True,
                        endpoint='https://configured.invalid/search',user_agent='test')
    with patch('requests.get',side_effect=AssertionError('network touched')):
        second, report=enrich([row],cache)
    assert second==first
    assert report['api_calls']==0


def test_reuses_database_coords_only_with_identity_and_correct_h3():
    row=provider()
    old=dict(source=SOURCE,external_id='stable',latitude=49.84,longitude=24.03,h3_id=h3.latlng_to_cell(49.84,24.03,9))
    assert reuse_existing([row],[old])[0]['coordinate_source']=='existing_database_identity'
    assert not reuse_existing([row],[dict(old,external_id='different')])[0].get('coordinate_source')
    assert not reuse_existing([row],[dict(old,h3_id='wrong')])[0].get('coordinate_source')


def test_inpatient_metadata_requires_provider_address_and_beds(tmp_path):
    row=provider()
    row.update(name='КНП Львівської обласної ради "Діагностичний центр"',sub_category='polyclinic')
    data=dict(name='КНПЛОРДіагностичнийцентр',settlement='Львів',address='Мечникова8',
              type_='лікарняний заклад однопрофільний або спеціалізований',cap_in=40,operation='так',GlobalID='profile')
    path=tmp_path/'profiles.geojson'
    path.write_text(json.dumps({'features':[{'properties':data,'geometry':{'coordinates':[0,0]}}]}),encoding='utf-8')
    result=apply_type_profiles([dict(row)],path)[0]
    assert result['sub_category']=='hospital'
    assert result['latitude']==row['latitude']  # GIS coordinates are not reused
    data['address']='Мечникова99'
    path.write_text(json.dumps({'features':[{'properties':data}]}),encoding='utf-8')
    assert apply_type_profiles([dict(row)],path)[0]['sub_category']=='polyclinic'
