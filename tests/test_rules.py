import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import Store, SpamGuard, RuleError, duration, can_assign, has_link


class Rules(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(duration('10m'),600)
        self.assertEqual(duration('2h'),7200)
        self.assertEqual(duration('7d'),604800)
        for text in ('0m','1s','forever','366d','-1h','1.5h'):
            with self.subTest(text=text),self.assertRaises(RuleError):
                duration(text)

    def test_roles_hierarchy(self):
        self.assertTrue(can_assign(5,0,4))
        self.assertTrue(can_assign(4,2,3))
        for values in ((4,4,1),(4,1,4),(3,0,1),(5,0,5),(5,5,0),(5,0,-1)):
            self.assertFalse(can_assign(*values))

    def test_links(self):
        for text in ('https://example.com','t.me/example','@example_bot','www.example.org','купите example.ru'):
            self.assertTrue(has_link(text,[]),text)
        self.assertTrue(has_link('скрытая ссылка',[SimpleNamespace(type='text_link')]))
        self.assertFalse(has_link('Привет, как дела?',[]))

    def test_spam_boundary_and_isolation(self):
        s=SpamGuard()
        self.assertFalse(s.hit(-1,1,now=100))
        for i in range(4):
            self.assertFalse(s.hit(-1,1,now=101+i))
        self.assertTrue(s.hit(-1,1,now=106))
        self.assertFalse(s.hit(-1,2,now=106))
        self.assertFalse(s.hit(-2,1,now=106))
        self.assertFalse(s.hit(-1,1,now=120))

    def test_spam_old_window_expires(self):
        s=SpamGuard()
        for i in range(12):
            self.assertFalse(s.hit(-1,1,now=100+i*10))


class Database(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.db'
        self.s=Store(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_roles_and_config_isolated(self):
        self.s.set_rank(-1,123,4)
        self.assertEqual(self.s.rank(-1,123),4)
        self.assertEqual(self.s.rank(-2,123),0)
        self.s.setting(-1,'links',False)
        self.assertFalse(self.s.config(-1)['links'])
        self.assertTrue(self.s.config(-2)['links'])
        with self.assertRaises(RuleError):
            self.s.set_rank(-1,123,5)

    def test_warnings(self):
        self.assertEqual(self.s.warn(-1,123),1)
        self.assertEqual(self.s.warn(-1,123),2)
        self.assertEqual(self.s.warn(-1,123,-1),1)
        self.s.clear_warns(-1,123)
        self.assertEqual(self.s.warns(-1,123),0)
        self.assertEqual(self.s.warn(-1,123,-1),0)

    def test_triggers(self):
        self.s.trigger(-1,' Привет ','Ответ')
        self.assertEqual(self.s.answer(-1,'ПРИВЕТ'),'Ответ')
        self.assertIsNone(self.s.answer(-2,'ПРИВЕТ'))
        self.assertIsNone(self.s.answer(-1,'ну привет'))
        self.s.trigger(-1,'Привет',None)
        self.assertIsNone(self.s.answer(-1,'привет'))

    def test_queue_persists_and_deduplicates(self):
        self.s.save_request(-1,123)
        self.s.save_request(-1,123)
        self.s.save_request(-2,234)
        self.s.close()
        self.s=Store(self.path)
        self.assertEqual(self.s.pending(-1),[123])
        self.s.drop_request(-1,123)
        self.assertEqual(self.s.pending(-1),[])
        self.assertEqual(self.s.pending(-2),[234])

    def test_audit(self):
        self.s.log(-1,123,456,'ban','test')
        self.assertEqual(self.s.logs(-1),[(123,456,'ban','test')])
        self.assertEqual(self.s.logs(-2),[])
