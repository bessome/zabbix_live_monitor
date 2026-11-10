"""SMTP notifications without contacting a real mail server."""
import os
import unittest
from unittest.mock import patch

from registration_mail import notify_activation, notify_registration


class RegistrationMailTests(unittest.TestCase):
    def test_activation_without_tls(self):
        env = {"SMTP_HOST": "relay.example.com", "SMTP_FROM": "monitor@stv.ee",
               "SMTP_SECURITY": "none", "SMTP_PORT": "25"}
        with patch.dict(os.environ, env, clear=True), patch("registration_mail.smtplib.SMTP") as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            notify_activation("newuser", "newuser@stv.ee")
            smtp.assert_called_once_with("relay.example.com", 25, timeout=15)
            client.starttls.assert_not_called()
            client.login.assert_not_called()
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["To"], "newuser@stv.ee")
            self.assertIn("активирована", message["Subject"])
            self.assertIn("newuser", message.get_content())

    def test_starttls_notification(self):
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "monitor@stv.ee",
               "SMTP_USERNAME": "monitor", "SMTP_PASSWORD": "smtp-secret"}
        with patch.dict(os.environ, env, clear=True), patch("registration_mail.smtplib.SMTP") as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.return_value = {}
            notify_registration("newuser", "newuser@stv.ee")
            smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
            client.starttls.assert_called_once()
            client.login.assert_called_once_with("monitor", "smtp-secret")
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["To"], "maksim@stv.ee")
            self.assertIn("newuser@stv.ee", message.get_content())
            self.assertIn("активации", message.get_content())
            self.assertNotIn("smtp-secret", message.as_string())

    def test_ssl_notification(self):
        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "monitor@stv.ee",
                                    "SMTP_SECURITY": "ssl"}, clear=True), patch("registration_mail.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            notify_registration("newuser", "newuser@stv.ee")
            self.assertEqual(smtp.call_args.args, ("smtp.example.com", 465))
            smtp.return_value.__enter__.return_value.starttls.assert_not_called()

    def test_unconfigured_notification(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                notify_registration("newuser", "newuser@stv.ee")
