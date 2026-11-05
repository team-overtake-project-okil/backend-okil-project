import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs.load_health import build_address, categorize
from app.jobs.health_pipeline import geocode, has_valid_coords, match_candidate, read_csv
from review_health import quality, apply


class HealthImportTests(unittest.TestCase):
    def test_category(self):
        self.assertEqual(categorize('Поліклінічне відділення', ''), 'polyclinic')
        self.assertEqual(categorize('Лікарня Святої Анни', ''), 'hospital')
        self.assertEqual(categorize('Адміністрація ТМО', ''), '')

    def test_address(self):
        addr = build_address({'addressPostName':'Львів','streetType':'вулиця','addressThoroughfare':'Пекарська','addressLocatorDesignator':'59а'})
        self.assertIn('Пекарська 59а', addr)

    def test_coordinates(self):
        self.assertTrue(has_valid_coords(49.874, 24.03))
        self.assertFalse(has_valid_coords(90, 24))

    def test_match_manual_only(self):
        source = {'address': 'Львів, вулиця Миколайчука І. 9, Львівська область, Україна'}
        hit = {'title':'Лікарня Святого Пантелеймона', 'address':'вулиця Івана Миколайчука, 9, Львів', 'gps_coordinates':{'latitude':49.8741774,'longitude':24.039617}}
        result, reason = match_candidate(source,[hit])
        self.assertIsNotNone(result)
        self.assertEqual(reason,'address_match_needs_manual_review')

    def test_rank_address(self):
        kind, similarity, rank = quality('Лікарня', 'Львів, вулиця Миколайчука І. 9, Львівська область, Україна', 'Лікарня', 'вулиця Івана Миколайчука, 9, Львів')
        self.assertEqual(kind,'address_match')
        self.assertGreater(rank,100)

    def test_dont_rank_wrong_house_as_match(self):
        self.assertEqual(quality('Лікарня','Львів, вулиця Пекарська 59а','Лікарня','вулиця Пекарська 59, Львів')[0],'manual_check')

    def test_apply_requires_selection(self):
        row={'source_key':'abc','source_row':'2','name':'Лікарня', 'address':'Львів, вулиця Пекарська 59а', 'sub_category':'hospital'}
        option={'source_key':'abc','selected':'no','category':'hospital','latitude':'49.84','longitude':'24.03'}
        reviewed,n = apply([row],[option])
        self.assertEqual(n,0)
        self.assertEqual(reviewed[0]['reviewed'],'no')

    def test_geocode_cached_and_shared_address(self):
        # This test uses a fixture cache, no external requests.
        import json, csv, hashlib
        with TemporaryDirectory() as temp:
            root=Path(temp)
            input_csv=root/'in.csv'; output=root/'out.csv'; cache_path=root/'cache.json'
            address='Львів, вулиця Миколайчука І. 9, Львівська область, Україна'
            rows=[{'source_key':'one','source_row':'2','name':'Об’єднання', 'address':address,'latitude':'','longitude':'','sub_category':''},
                  {'source_key':'two','source_row':'3','name':'Лікарня', 'address':address,'latitude':'','longitude':'','sub_category':'hospital'}]
            with input_csv.open('w',encoding='utf-8',newline='') as f:
                wr=csv.DictWriter(f,fieldnames=rows[0].keys()); wr.writeheader(); wr.writerows(rows)
            key=hashlib.sha256((rows[0]['name']+'|'+address).encode()).hexdigest()
            cache_path.write_text(json.dumps({key:[{'title':'Лікарня','address':'вулиця Івана Миколайчука, 9, Львів','gps_coordinates':{'latitude':49.8741774,'longitude':24.039617}}]}, ensure_ascii=False), encoding='utf-8')
            report=geocode(input_csv,output,cache_path,0,False)
            self.assertEqual(report['api_calls'],0)
            self.assertEqual(report['candidates_for_review'],2)
            self.assertTrue(all(r['reviewed']=='no' for r in read_csv(output)))


if __name__=='__main__': unittest.main()
