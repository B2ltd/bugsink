from django.test import SimpleTestCase

from issues.utils import get_title_for_exception_type_and_value, get_type_and_value_for_data


class DisplayTitleTestCase(SimpleTestCase):
    def test_exception_title_contains_type_and_value(self):
        self.assertEqual(
            "ValueError: invalid value",
            get_title_for_exception_type_and_value("ValueError", "invalid value"),
        )

    def test_log_message_title_is_the_message(self):
        self.assertEqual(
            "Could not reach upstream",
            get_title_for_exception_type_and_value("Log Message", "Could not reach upstream"),
        )

    def test_formatted_log_message_is_the_display_value(self):
        self.assertEqual(
            ("Log Message", "foo bar"),
            get_type_and_value_for_data({"logentry": {
                "message": "foo %s",
                "formatted": "foo bar",
            }}),
        )

    def test_unformatted_log_message_is_the_display_fallback(self):
        self.assertEqual(
            ("Log Message", "foo %s"),
            get_type_and_value_for_data({"logentry": {"message": "foo %s"}}),
        )
