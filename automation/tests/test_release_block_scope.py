"""Keep dates for later patch contents separate from the patch release itself."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import parse_article

class ReleaseBlockScopeTests(unittest.TestCase):
    def article(self, text):
        return {'gameId':'endfield', 'url':'https://endfield.gryphline.com/en-us/news/9560',
                'title':'[Sanctuary of Ink] Version Update Notes',
                'publishedOn':'2026-10-15', 'text':text}

    def test_secondary_release_time_is_not_patch_schedule(self):
        text = ('■ Update & Maintenance Time\n\n'
                'Asia Server: Oct. 15, 2026 at 06:00 – Oct. 15, 2026 at 12:00 (UTC+8)\n\n'
                '■ Update Notes\n\nA new feature is included.\n\n'
                'Release Time\nNov. 12, 2026 at 12:00 (server time)')
        value = parse_article(self.article(text), 'title')[0]
        self.assertEqual(value['date'], '2026-10-15')
        self.assertEqual(value['warnings'], [])

    def test_secondary_valid_date_not_misread_as_conflict(self):
        text = ('Maintenance begins October 15, 2026 at 06:00 (UTC+8).\n'
                '■ Update Notes\nNew quest.\nRelease Date\nNovember 12, 2026')
        value = parse_article(self.article(text), 'title')[0]
        self.assertEqual(value['date'], '2026-10-15')
        self.assertFalse(value['warnings'])

    def test_primary_generic_release_header_still_accepted(self):
        value = parse_article(self.article('Release Date\nOctober 15, 2026\n■ Update Notes\nNew content.'), 'title')[0]
        self.assertEqual(value['date'], '2026-10-15')

    def test_two_primary_conflicting_date_headers_still_fail(self):
        value = parse_article(self.article('Release Date\nOctober 15, 2026\nUpdate Date\nOctober 16, 2026'), 'title')[0]
        self.assertIsNone(value['date'])
        self.assertTrue(value['warnings'])

    def test_late_explicit_version_header_still_accepted(self):
        value = self.article('Intro.\n' * 400 + 'Version 3.3 Update Date\nOctober 21, 2026')
        value.update(gameId='zzz', title='Version 3.3 Special Program')
        self.assertEqual(parse_article(value)[0]['date'], '2026-10-21')

    def test_late_explicit_release_statement_still_accepted(self):
        value = self.article('■ Update Notes\nNew content.\nSanctuary of Ink will officially launch on October 15, 2026.')
        self.assertEqual(parse_article(value,'title')[0]['date'], '2026-10-15')

if __name__ == '__main__':
    unittest.main()
