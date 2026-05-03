import base64
import unittest
from email.message import EmailMessage

from smtp2worker import build_payload, extract_path


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


if __name__ == "__main__":
    unittest.main()
