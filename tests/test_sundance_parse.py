"""Issue #112: the Sundance parser missed two winners the #33 accuracy audit found.
  - "Blood Brother" (2013 U.S. Documentary): the title is unlinked on Wikipedia, sundance_documentary() only read linked ones.
  - "What Happened Was" (1994 U.S. Dramatic): "''[[What Happened Was]]...''" - the "..." after the link broke the pattern.
Wikitext lines are copied from the live pages; wikitext() is stubbed (no network, no TMDB)."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import awards_wiki  # noqa: E402

DOCUMENTARY = """==U.S. winners==
*[[2012 Sundance Film Festival|2012]]: ''[[The House I Live In (2012 film)|The House I Live In]]''<ref>x</ref>
*[[2013 Sundance Film Festival|2013]]: ''Blood Brother''
*[[2014 Sundance Film Festival|2014]]: ''[[Rich Hill (film)|Rich Hill]]''
==International winners==
*2012: ''[[The Law in These Parts]]''
"""
DRAMATIC = """{|
|-
! Year !! Film !! Director !! Ref
|-
| style="text-align:center;"| 1993 || ''[[Public Access (1993 film)|Public Access]]'' || [[Bryan Singer]] || <ref>x</ref>
|-
| style="text-align:center;"| 1994 || ''[[What Happened Was]]...'' || [[Tom Noonan]] || <ref>x</ref>
|-
| style="text-align:center;"| 1995 || ''[[The Brothers McMullen]]'' || [[Edward Burns]] || <ref>x</ref>
|}
"""


class SundanceParse(unittest.TestCase):
    def test_unlinked_documentary_title(self):          # replays the Blood Brother miss
        with mock.patch.object(awards_wiki, 'wikitext', return_value=DOCUMENTARY):
            got = awards_wiki.sundance_documentary()
        self.assertIn((2013, 'Blood Brother'), [(y, t) for y, t, _, _ in got])
        self.assertEqual([y for y, _, _, _ in got], [2012, 2013, 2014])      # linked rows and the International cut intact

    def test_title_with_text_after_the_link(self):      # replays the What Happened Was miss
        with mock.patch.object(awards_wiki, 'wikitext', return_value=DRAMATIC):
            got = awards_wiki.sundance_dramatic()
        self.assertEqual([(y, t) for y, t, _, _ in got],
                         [(1993, 'Public Access'), (1994, 'What Happened Was'), (1995, 'The Brothers McMullen')])
        self.assertEqual(got[1][3], 'What Happened Was')                     # the link target still drives the TMDB hint


if __name__ == '__main__':
    unittest.main()
