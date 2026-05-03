import base64
import os
import tempfile
import unittest
from email.message import EmailMessage

from smtp2worker import build_payload, extract_path, load_dotenv, parse_dotenv_value


class PayloadTests(unittest.TestCase):
    def test_extracts_plain_text_message(self):
        message = EmailMessage()
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "user@example.net"
        message["Subject"] = "Code 123456"
        message.set_content("Your code is 123456\n")

        payload = build_payload(
            raw_message=message.as_bytes(),
            mail_from="sender@example.com",
            rcpt_to=["user@example.net"],
            helo="app.local",
            peer="127.0.0.1",
        )

        self.assertEqual(payload["envelope"]["from"], "sender@example.com")
        self.assertEqual(payload["envelope"]["to"], ["user@example.net"])
        self.assertEqual(payload["message"]["subject"], "Code 123456")
        self.assertIn("123456", payload["message"]["text"])
        self.assertTrue(base64.b64decode(payload["message"]["rawBase64"]))

    def test_extracts_html_alternative(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["To"] = "user@example.net"
        message["Subject"] = "Code"
        message.set_content("plain")
        message.add_alternative("<strong>html</strong>", subtype="html")

        payload = build_payload(
            raw_message=message.as_bytes(),
            mail_from="sender@example.com",
            rcpt_to=["user@example.net"],
            helo=None,
            peer="127.0.0.1",
        )

        self.assertEqual(payload["message"]["text"], "plain")
        self.assertIn("<strong>html</strong>", payload["message"]["html"])


class AddressTests(unittest.TestCase):
    def test_extracts_angle_path(self):
        self.assertEqual(extract_path("<user@example.com> SIZE=123"), "user@example.com")

    def test_extracts_bare_path(self):
        self.assertEqual(extract_path("user@example.com"), "user@example.com")

    def test_rejects_unclosed_angle_path(self):
        self.assertIsNone(extract_path("<user@example.com"))


class DotenvTests(unittest.TestCase):
    def test_parses_quoted_value(self):
        self.assertEqual(parse_dotenv_value('"line\\nnext"'), "line\nnext")
        self.assertEqual(parse_dotenv_value("'literal'"), "literal")

    def test_loads_dotenv_without_overriding_existing_environment(self):
        previous = os.environ.get("SMTP2WORKER_TEST_VALUE")
        os.environ["SMTP2WORKER_TEST_VALUE"] = "from-env"
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as file:
                file.write("SMTP2WORKER_TEST_VALUE=from-file\n")
                file.write("SMTP2WORKER_TEST_OTHER=\"hello\"\n")
                path = file.name
            try:
                self.assertTrue(load_dotenv(path))
                self.assertEqual(os.environ["SMTP2WORKER_TEST_VALUE"], "from-env")
                self.assertEqual(os.environ["SMTP2WORKER_TEST_OTHER"], "hello")
            finally:
                os.unlink(path)
                os.environ.pop("SMTP2WORKER_TEST_OTHER", None)
        finally:
            if previous is None:
                os.environ.pop("SMTP2WORKER_TEST_VALUE", None)
            else:
                os.environ["SMTP2WORKER_TEST_VALUE"] = previous


if __name__ == "__main__":
    unittest.main()
