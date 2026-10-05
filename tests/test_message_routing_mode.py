import unittest
from dataclasses import replace

from config import AppSettings, DeepSeekSettings, FeishuSettings
from service.runtime import _build_infrastructure


class MessageRoutingModeTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            archive_parent_folder_token="folder",
            message_override_open_id="ou_test",
        )

    def test_formal_mode_uses_actual_recipient(self):
        messages = _build_infrastructure(self.settings).messages
        self.assertEqual(messages.recipient_override, "")

    def test_simulation_mode_uses_env_recipient(self):
        settings = replace(self.settings, simulation_mode=True)
        settings.validate()
        messages = _build_infrastructure(settings).messages
        self.assertEqual(messages.recipient_override, "ou_test")

    def test_simulation_requires_test_recipient(self):
        settings = replace(self.settings, simulation_mode=True,
                           message_override_open_id="")
        with self.assertRaises(ValueError):
            settings.validate()
