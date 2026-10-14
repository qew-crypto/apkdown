import unittest
from auth import normalize_code
import test_auth as auth_tests


class CodeFormat(unittest.TestCase):
    def test_dotted(self):
        self.assertEqual(normalize_code('1.2.3.5'), '1235')
        self.assertEqual(normalize_code('0.1.2.3.4'), '01234')
        self.assertEqual(normalize_code('1.2.3.4.5.6.7.8'), '12345678')

    def test_plain(self):
        self.assertEqual(normalize_code('01234'), '01234')
        self.assertEqual(normalize_code('  1.2.3.5\n'), '1235')

    def test_reject_malformed(self):
        for value in ('1..2.3.5', '.1.2.3.5', '1.2.3.5.', '12.35', '1-2-3-5',
                      '1.2.3', '123456789', '', 'abcd', '１２３４'):
            with self.subTest(value=value):
                self.assertIsNone(normalize_code(value))


class DottedConversation(unittest.IsolatedAsyncioTestCase):
    setUp = auth_tests.LoginTests.setUp
    event = auth_tests.LoginTests.event
    begin = auth_tests.LoginTests.begin

    async def test_dotted_code_reaches_telegram_without_dots(self):
        await self.begin()
        message = self.event('0.1.2.3.4')
        await self.login.handle(message)
        message.delete.assert_awaited_once()
        self.user.sign_in.assert_awaited_once_with(
            phone='+79990000000', code='01234', phone_code_hash='test-hash')
        self.assertIsNone(self.login.stage)

    async def test_malformed_code_not_submitted(self):
        await self.begin()
        await self.login.handle(self.event('1..2.3.4'))
        self.user.sign_in.assert_not_awaited()
        self.assertEqual(self.login.stage, 'code')
